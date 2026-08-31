"""Read-only canonical lane replay and exact per-seed PROV-O closure.

This module is intentionally domain-neutral. A versioned benchmark definition
supplies the literal query, candidate horizons, K values, embedding model and
scope settings. It runs inside the backend runtime without changing any
retrieval, index, workspace or deployment configuration.
"""

from __future__ import annotations

import asyncio
import base64
import json
import resource
import sys
import time
from typing import Any

SOURCE_FIELDS = [
    "chunk_id",
    "document_id",
    "filename",
    "mimetype",
    "page",
    "chunk_index",
    "chunking_strategy",
    "text",
    "source_url",
    "source_provenance",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_entity_alternate_ids",
    "source_relation_target_ids",
    "source_relation_roles",
    "source_relative_path",
    "source_path_ancestors",
    "connector_file_id",
    "owner",
    "owner_name",
    "owner_email",
    "file_size",
    "connector_type",
    "embedding_model",
    "embedding_dimensions",
    "parser",
    "chunk_size",
    "chunk_overlap",
    "document_profile_version",
    "document_chunk_count",
    "document_page_count",
    "document_max_page",
    "document_character_count",
    "document_size_class",
    "allowed_users",
    "allowed_groups",
    "allowed_principal_labels",
]


def _lexical_body(query: str, size: int) -> dict[str, Any]:
    return {
        "query": {
            "bool": {
                "should": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["text^2", "filename^1.5"],
                            "type": "best_fields",
                            "operator": "or",
                            "fuzziness": "AUTO:4,7",
                        }
                    },
                    {
                        "match_phrase_prefix": {
                            "text": {"query": query, "max_expansions": 50}
                        }
                    },
                ],
                "minimum_should_match": 1,
                "filter": [],
            }
        },
        "_source": SOURCE_FIELDS,
        "size": size,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
    }


def _dense_body(vector_field: str, vector: list[float], size: int) -> dict[str, Any]:
    return {
        "query": {
            "bool": {
                "should": [
                    {
                        "dis_max": {
                            "tie_breaker": 0.0,
                            "queries": [
                                {
                                    "knn": {
                                        vector_field: {
                                            "vector": vector,
                                            "k": size,
                                            "num_candidates": max(1000, size),
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ],
                "minimum_should_match": 1,
                "filter": [{"exists": {"field": vector_field}}],
            }
        },
        "_source": SOURCE_FIELDS,
        "size": size,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
    }


def _without_num_candidates(body: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(body))

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            item.pop("num_candidates", None)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return value


def _flatten_hit(hit: dict[str, Any], ranks: dict[str, int]) -> dict[str, Any]:
    source = dict(hit.get("_source", {}))
    source["chunk_id"] = source.get("chunk_id") or hit.get("_id")
    source["score"] = (
        hit.get("_retrieval_rerank_score")
        or hit.get("_retrieval_fusion_score")
        or hit.get("_score")
    )
    source.update(ranks)
    return source


def _compact_hit(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "chunk_id",
        "document_id",
        "filename",
        "mimetype",
        "chunk_index",
        "source_entity_id",
        "source_entity_type",
        "source_entity_system",
        "embedding_model",
        "embedding_dimensions",
        "document_chunk_count",
        "score",
        "lexical_rank",
        "dense_rank",
        "rrf_rank",
        "lexical_score",
        "dense_score",
        "rrf_score",
    )
    return {field: item.get(field) for field in fields}


def _compact_document(item: dict[str, Any]) -> dict[str, Any]:
    return {
        field: item.get(field)
        for field in (
            "document_id",
            "filename",
            "source_entity_id",
            "source_entity_type",
            "source_entity_system",
            "complete",
            "status_code",
        )
    }


async def _timed_search(client: Any, index_name: str, body: dict[str, Any]) -> tuple[Any, float]:
    from opensearchpy.exceptions import RequestError

    started = time.perf_counter()
    try:
        response = await client.search(
            index=index_name,
            body=body,
            params={"terminate_after": 0},
        )
    except RequestError as exc:
        if "unknown field [num_candidates]" not in str(exc).lower():
            raise
        response = await client.search(
            index=index_name,
            body=_without_num_candidates(body),
            params={"terminate_after": 0},
        )
    return response, time.perf_counter() - started


async def run(plan: dict[str, Any]) -> dict[str, Any]:
    from config.settings import clients, get_embedding_model, get_index_name, get_openrag_config
    from services.models_service import ModelsService
    from services.retrieval_service import (
        ScopeExhaustiveSettings,
        hit_identity,
        limit_chunks_per_document,
        reciprocal_rank_fusion,
    )
    from services.search_service import SearchService
    from session_manager import SessionManager
    from utils.embedding_fields import get_embedding_field_name

    query = str(plan["query"])
    if query != str(plan.get("query_actually_executed", query)):
        raise ValueError("query literal and query_actually_executed differ")
    retrieval = plan["retrieval"]
    lexical_size = int(retrieval["lexical_candidates"])
    dense_size = int(retrieval["vector_candidates"])
    rrf_k = int(retrieval["rrf_k"])
    base_cap = int(retrieval["max_chunks_per_document"])
    adaptive_cap = int(retrieval["adaptive_max_chunks_per_document"])
    if retrieval.get("reranker_enabled"):
        raise ValueError("this baseline runner requires the declared reranker to be disabled")

    session_manager = SessionManager()
    search_service = SearchService(session_manager, ModelsService())
    client = session_manager.get_user_opensearch_client("anonymous", None)
    index_name = get_index_name()
    knowledge = get_openrag_config().knowledge
    provider = str(knowledge.embedding_provider)
    embedding_model = str(plan.get("embedding_model") or get_embedding_model())
    formatted_model = await ModelsService().get_litellm_model_name(
        embedding_model,
        provider=provider,
    )

    embedding_started = time.perf_counter()
    embedded = await clients.patched_embedding_client.embeddings.create(
        model=formatted_model,
        input=[query],
    )
    embedding_seconds = time.perf_counter() - embedding_started
    vector = getattr(embedded.data[0], "embedding", None)
    if vector is None:
        vector = embedded.data[0]["embedding"]
    vector_field = get_embedding_field_name(embedding_model)

    lexical_task = _timed_search(client, index_name, _lexical_body(query, lexical_size))
    dense_task = _timed_search(
        client,
        index_name,
        _dense_body(vector_field, vector, dense_size),
    )
    (lexical_response, lexical_seconds), (dense_response, dense_seconds) = await asyncio.gather(
        lexical_task,
        dense_task,
    )
    lexical_raw = lexical_response.get("hits", {}).get("hits", [])
    dense_raw = dense_response.get("hits", {}).get("hits", [])
    lexical_rank = {hit_identity(hit): rank for rank, hit in enumerate(lexical_raw, start=1)}
    dense_rank = {hit_identity(hit): rank for rank, hit in enumerate(dense_raw, start=1)}
    lexical_score = {hit_identity(hit): hit.get("_score") for hit in lexical_raw}
    dense_score = {hit_identity(hit): hit.get("_score") for hit in dense_raw}

    fusion_started = time.perf_counter()
    lane_raw = {
        "lexical": reciprocal_rank_fusion([lexical_raw], k=rrf_k),
        "dense": reciprocal_rank_fusion([dense_raw], k=rrf_k),
        "rrf": reciprocal_rank_fusion([lexical_raw, dense_raw], k=rrf_k),
    }
    ranked_hits: dict[str, list[dict[str, Any]]] = {}
    for mode, hits in lane_raw.items():
        limited = limit_chunks_per_document(
            hits,
            max_chunks_per_document=base_cap,
            adaptive_max_chunks_per_document=adaptive_cap,
        )
        flattened: list[dict[str, Any]] = []
        for final_rank, hit in enumerate(limited, start=1):
            identity = hit_identity(hit)
            ranks: dict[str, Any] = {
                "lexical_rank": lexical_rank.get(identity),
                "dense_rank": dense_rank.get(identity),
                "lexical_score": lexical_score.get(identity),
                "dense_score": dense_score.get(identity),
            }
            if mode == "rrf":
                ranks["rrf_rank"] = final_rank
                ranks["rrf_score"] = hit.get("_retrieval_fusion_score")
            flattened.append(_flatten_hit(hit, ranks))
        ranked_hits[mode] = flattened
    fusion_seconds = time.perf_counter() - fusion_started

    scope_plan = plan.get("scope", {})
    scope_settings = ScopeExhaustiveSettings(
        seed_count=max(int(value) for value in plan["k_values"]),
        max_depth=int(scope_plan.get("max_depth", 8)),
        max_entities=int(scope_plan.get("max_entities", 500)),
        max_documents=int(scope_plan.get("max_documents", 250)),
        batch_size=int(scope_plan.get("batch_size", 50)),
    )
    closures: list[dict[str, Any]] = []
    closure_cache: dict[tuple[str, ...], dict[str, Any]] = {}
    for mode in ("lexical", "dense", "rrf"):
        for requested_k in plan["k_values"]:
            seeds = ranked_hits[mode][: int(requested_k)]
            cache_key = tuple(str(item.get("chunk_id") or "") for item in seeds)
            if cache_key in closure_cache:
                cached = closure_cache[cache_key]
                closures.append(
                    {
                        **cached,
                        "mode": mode,
                        "requested_k": int(requested_k),
                        "effective_k": len(seeds),
                        "reused_for_identical_seed_set": True,
                    }
                )
                continue

            def fixed_search_tool_for(
                bound_seeds: list[dict[str, Any]],
            ) -> Any:
                async def fixed_search_tool(
                    _query: str, embedding_model: str | None = None, **_kwargs: Any
                ) -> dict[str, Any]:
                    del embedding_model
                    return {"results": bound_seeds}

                return fixed_search_tool

            search_service.search_tool = fixed_search_tool_for(seeds)
            closure_started = time.perf_counter()
            closure = await search_service.search_exhaustive_scope(
                query,
                user_id="anonymous",
                jwt_token=None,
                filters=None,
                embedding_model=embedding_model,
                settings=scope_settings,
            )
            closure_seconds = time.perf_counter() - closure_started
            compact = {
                "mode": mode,
                "requested_k": int(requested_k),
                "effective_k": len(seeds),
                "scope_closure_seconds": closure_seconds,
                "reused_for_identical_seed_set": False,
                "documents": [
                    _compact_document(item)
                    for item in closure.get("documents", [])
                    if isinstance(item, dict)
                ],
                "coverage": closure.get("coverage", {}),
                "graph_summary": {
                    "entities": len(closure.get("graph", {}).get("entities", [])),
                    "edges": len(closure.get("graph", {}).get("edges", [])),
                    "context_edges": len(closure.get("graph", {}).get("context_edges", [])),
                },
            }
            closure_cache[cache_key] = compact
            closures.append(compact)

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "schema_version": 1,
        "query_literal": query,
        "query_actually_executed": query,
        "index": index_name,
        "embedding_provider": provider,
        "embedding_model": embedding_model,
        "embedding_dimensions": len(vector),
        "retrieval": retrieval,
        "runtime_config_observed": {
            "retrieval_strategy": getattr(knowledge, "retrieval_strategy", None),
            "retrieval_mode": getattr(knowledge, "retrieval_mode", None),
            "retrieval_lexical_candidates": getattr(
                knowledge, "retrieval_lexical_candidates", None
            ),
            "retrieval_vector_candidates": getattr(
                knowledge, "retrieval_vector_candidates", None
            ),
            "retrieval_rrf_k": getattr(knowledge, "retrieval_rrf_k", None),
            "retrieval_max_chunks_per_document": getattr(
                knowledge, "retrieval_max_chunks_per_document", None
            ),
            "retrieval_adaptive_max_chunks_per_document": getattr(
                knowledge, "retrieval_adaptive_max_chunks_per_document", None
            ),
            "retrieval_reranker_url": getattr(knowledge, "retrieval_reranker_url", None),
            "embedding_provider": getattr(knowledge, "embedding_provider", None),
            "embedding_model": get_embedding_model(),
            "chunking_strategy": getattr(knowledge, "chunking_strategy", None),
            "hybrid_max_tokens": getattr(knowledge, "hybrid_max_tokens", None),
            "hybrid_merge_peers": getattr(knowledge, "hybrid_merge_peers", None),
        },
        "k_values": plan["k_values"],
        "candidate_horizons": {
            "lexical_raw": len(lexical_raw),
            "dense_raw": len(dense_raw),
            "lexical_after_document_limit": len(ranked_hits["lexical"]),
            "dense_after_document_limit": len(ranked_hits["dense"]),
            "rrf_after_document_limit": len(ranked_hits["rrf"]),
        },
        "discovery_latency_seconds": {
            "embedding": embedding_seconds,
            "lexical": lexical_seconds,
            "dense_search_only": dense_seconds,
            "dense_including_embedding": embedding_seconds + dense_seconds,
            "rrf_fusion": fusion_seconds,
            "rrf_parallel_including_embedding": embedding_seconds
            + max(lexical_seconds, dense_seconds)
            + fusion_seconds,
        },
        "lanes": {
            mode: [_compact_hit(item) for item in hits]
            for mode, hits in ranked_hits.items()
        },
        "closures": closures,
        "process_resource_usage": {
            "max_rss_kib": usage.ru_maxrss,
            "user_cpu_seconds": usage.ru_utime,
            "system_cpu_seconds": usage.ru_stime,
        },
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected one base64-encoded JSON plan")
    plan = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    result = asyncio.run(run(plan))
    print("BASELINE_RESULT_JSON=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
