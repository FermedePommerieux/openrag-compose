"""Read-only OpenSearch lane capture for discovery control benchmarks.

The module contains no benchmark-domain vocabulary.  A versioned benchmark
definition supplies every query and metadata anchor.  It is intended to run in
the backend runtime so it can reuse the current embedding provider and the
anonymous/user-scoped OpenSearch client without changing runtime settings.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from collections import defaultdict
from typing import Any

SOURCE_FIELDS = [
    "chunk_id",
    "document_id",
    "filename",
    "mimetype",
    "page",
    "chunk_index",
    "text",
    "source_url",
    "source_provenance",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_entity_alternate_ids",
    "source_relation_target_ids",
    "source_relation_roles",
    "connector_type",
    "embedding_model",
    "embedding_dimensions",
]


def _hit_identity(hit: dict[str, Any]) -> str:
    source = hit.get("_source", {})
    return str(source.get("chunk_id") or hit.get("_id") or "")


def _occurrence_identity(source: dict[str, Any]) -> str:
    return str(source.get("source_entity_id") or source.get("document_id") or "")


def _compact_ranked_hits(
    hits: list[dict[str, Any]], lane: str, *, occurrence_limit: int | None = None
) -> list[dict[str, Any]]:
    """Keep the best-ranked chunk for each source occurrence."""
    compact: list[dict[str, Any]] = []
    seen: set[str] = set()
    occurrence_rank = 0
    for chunk_rank, hit in enumerate(hits, start=1):
        source = hit.get("_source", {})
        occurrence_id = _occurrence_identity(source)
        if not occurrence_id or occurrence_id in seen:
            continue
        seen.add(occurrence_id)
        occurrence_rank += 1
        compact_source = dict(source)
        if isinstance(compact_source.get("text"), str):
            compact_source["text"] = compact_source["text"][:2_000]
        compact.append(
            {
                **compact_source,
                "occurrence_id": occurrence_id,
                f"{lane}_rank": chunk_rank,
                f"{lane}_occurrence_rank": occurrence_rank,
                f"{lane}_score": hit.get("_score"),
            }
        )
        if occurrence_limit is not None and occurrence_rank >= occurrence_limit:
            break
    return compact


def _rrf_hits(
    lexical_hits: list[dict[str, Any]],
    dense_hits: list[dict[str, Any]],
    *,
    rrf_k: int,
) -> list[dict[str, Any]]:
    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, dict[str, Any]] = {}
    for ranked in (lexical_hits, dense_hits):
        for rank, hit in enumerate(ranked, start=1):
            identity = _hit_identity(hit)
            if not identity:
                continue
            scores[identity] += 1.0 / (rrf_k + rank)
            by_id.setdefault(identity, hit)
    ordered = sorted(by_id, key=lambda identity: (-scores[identity], identity))
    return [
        {**by_id[identity], "_score": scores[identity]} for identity in ordered
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
            }
        },
        "_source": SOURCE_FIELDS,
        "size": size,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
        "track_total_hits": True,
    }


def _dense_body(vector_field: str, vector: list[float], size: int) -> dict[str, Any]:
    return {
        "query": {
            "bool": {
                "should": [
                    {
                        "knn": {
                            vector_field: {
                                "vector": vector,
                                "k": size,
                            }
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
        "track_total_hits": True,
    }


def _metadata_body(anchors: list[str], size: int) -> dict[str, Any]:
    return {
        "query": {
            "bool": {
                "should": [
                    {"terms": {"source_entity_id": anchors}},
                    {"terms": {"source_entity_alternate_ids": anchors}},
                    {"terms": {"source_relation_target_ids": anchors}},
                ],
                "minimum_should_match": 1,
            }
        },
        "_source": SOURCE_FIELDS,
        "size": size,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
        "track_total_hits": True,
    }


def _metadata_terms_body(field: str, values: list[str], size: int) -> dict[str, Any]:
    return {
        "query": {"terms": {field: values}},
        "_source": SOURCE_FIELDS,
        "size": size,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
        "track_total_hits": True,
    }


async def run(plan: dict[str, Any]) -> dict[str, Any]:
    """Execute a generic lane plan using the runtime's DLS-scoped client."""
    from config.settings import (
        clients,
        get_embedding_model,
        get_index_name,
        get_openrag_config,
    )
    from services.models_service import ModelsService
    from session_manager import SessionManager
    from utils.embedding_fields import get_embedding_field_name

    session_manager = SessionManager()
    client = session_manager.get_user_opensearch_client("anonymous", None)
    index_name = get_index_name()
    configured_model = get_embedding_model() or "text-embedding-3-large"
    provider = get_openrag_config().knowledge.embedding_provider
    embedding_model = str(plan.get("embedding_model") or configured_model)
    formatted_model = await ModelsService().get_litellm_model_name(
        embedding_model,
        provider=str(provider),
    )
    lexical_size = max(1, min(10_000, int(plan.get("lexical_size", 500))))
    dense_size = max(1, min(10_000, int(plan.get("dense_size", 500))))
    rrf_k = max(1, int(plan.get("rrf_k", 60)))

    captures: list[dict[str, Any]] = []
    for query_spec in plan.get("queries", []):
        query = str(query_spec["text"])
        lanes = set(query_spec.get("lanes", ["lexical"]))
        capture_horizon = max(
            1,
            int(
                query_spec.get(
                    "inspection_horizon", query_spec.get("review_horizon", 100)
                )
            ),
        )
        lexical_hits: list[dict[str, Any]] = []
        dense_hits: list[dict[str, Any]] = []
        if lanes & {"lexical", "rrf"}:
            response = await client.search(
                index=index_name,
                body=_lexical_body(query, lexical_size),
                params={"terminate_after": 0},
            )
            lexical_hits = response.get("hits", {}).get("hits", [])
            captures.append(
                {
                    **query_spec,
                    "lane": "lexical",
                    "raw_chunk_hits": len(lexical_hits),
                    "total_chunk_hits": response.get("hits", {}).get("total"),
                    "results": _compact_ranked_hits(
                        lexical_hits,
                        "lexical",
                        occurrence_limit=capture_horizon,
                    ),
                }
            )
        if lanes & {"dense", "rrf"}:
            embedded = await clients.patched_embedding_client.embeddings.create(
                model=formatted_model,
                input=[query],
            )
            vector = getattr(embedded.data[0], "embedding", None)
            if vector is None:
                vector = embedded.data[0]["embedding"]
            vector_field = get_embedding_field_name(embedding_model)
            response = await client.search(
                index=index_name,
                body=_dense_body(vector_field, vector, dense_size),
                params={"terminate_after": 0},
            )
            dense_hits = response.get("hits", {}).get("hits", [])
            captures.append(
                {
                    **query_spec,
                    "lane": "dense",
                    "embedding_model": embedding_model,
                    "raw_chunk_hits": len(dense_hits),
                    "total_chunk_hits": response.get("hits", {}).get("total"),
                    "results": _compact_ranked_hits(
                        dense_hits,
                        "dense",
                        occurrence_limit=capture_horizon,
                    ),
                }
            )
        if "rrf" in lanes and lexical_hits and dense_hits:
            fused = _rrf_hits(lexical_hits, dense_hits, rrf_k=rrf_k)
            captures.append(
                {
                    **query_spec,
                    "lane": "rrf",
                    "rrf_k": rrf_k,
                    "raw_chunk_hits": len(fused),
                    "results": _compact_ranked_hits(
                        fused,
                        "rrf",
                        occurrence_limit=capture_horizon,
                    ),
                }
            )

    anchors = [str(value) for value in plan.get("metadata_anchors", []) if value]
    if anchors:
        metadata_size = max(1, min(10_000, int(plan.get("metadata_size", 10_000))))
        response = await client.search(
            index=index_name,
            body=_metadata_body(anchors, metadata_size),
            params={"terminate_after": 0},
        )
        metadata_hits = response.get("hits", {}).get("hits", [])
        captures.append(
            {
                "pass_id": "metadata-recovery",
                "query_id": "known-component-anchors",
                "text": "",
                "lane": "metadata_relation",
                "raw_chunk_hits": len(metadata_hits),
                "total_chunk_hits": response.get("hits", {}).get("total"),
                "results": _compact_ranked_hits(metadata_hits, "metadata_relation"),
            }
        )

    for search_spec in plan.get("metadata_searches", []):
        values = [str(value) for value in search_spec.get("values", []) if value]
        field = str(search_spec.get("field") or "")
        if not field or not values:
            continue
        size = max(
            1,
            min(
                10_000,
                int(search_spec.get("review_horizon", plan.get("metadata_size", 10_000))),
            ),
        )
        response = await client.search(
            index=index_name,
            body=_metadata_terms_body(field, values, size),
            params={"terminate_after": 0},
        )
        hits = response.get("hits", {}).get("hits", [])
        captures.append(
            {
                **search_spec,
                "lane": "metadata_terms",
                "raw_chunk_hits": len(hits),
                "total_chunk_hits": response.get("hits", {}).get("total"),
                "results": _compact_ranked_hits(
                    hits,
                    "metadata_terms",
                    occurrence_limit=size,
                ),
            }
        )

    return {
        "schema_version": 1,
        "index": index_name,
        "embedding_model": embedding_model,
        "lexical_size": lexical_size,
        "dense_size": dense_size,
        "rrf_k": rrf_k,
        "captures": captures,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected one base64-encoded JSON plan")
    plan = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    result = asyncio.run(run(plan))
    print("CONTROL_RESULT_JSON=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
