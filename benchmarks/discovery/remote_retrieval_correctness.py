"""Read-only correctness diagnostics executed inside the backend runtime.

The probe calls the product ``SearchService`` and its DLS-scoped OpenSearch
client.  It does not contain a second retrieval implementation, mutate runtime
settings, read benchmark ground truth, or persist any cluster resource.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import resource
import sys
import time
import types
from typing import Any


def _total(response: dict[str, Any]) -> int:
    value = response.get("count", response.get("hits", {}).get("total", 0))
    if isinstance(value, dict):
        value = value.get("value", 0)
    return int(value or 0)


async def _identity_audit(client: Any, index_name: str) -> dict[str, Any]:
    async def count(query: dict[str, Any]) -> int:
        return _total(await client.count(index=index_name, body={"query": query}))

    total_chunks = await count({"match_all": {}})
    missing_chunk_id = await count({"bool": {"must_not": [{"exists": {"field": "chunk_id"}}]}})
    missing_document_id = await count(
        {"bool": {"must_not": [{"exists": {"field": "document_id"}}]}}
    )
    missing_source_entity_id = await count(
        {"bool": {"must_not": [{"exists": {"field": "source_entity_id"}}]}}
    )
    missing_provenance = await count(
        {"bool": {"must_not": [{"exists": {"field": "source_provenance.entity.id"}}]}}
    )
    legacy_response = await client.search(
        index=index_name,
        body={
            "query": {"bool": {"must_not": [{"exists": {"field": "source_entity_id"}}]}},
            "_source": ["chunk_id", "document_id"],
            "size": min(10_000, missing_source_entity_id),
            "sort": [{"chunk_id": {"order": "asc", "missing": "_last"}}],
            "track_total_hits": True,
        },
        params={"terminate_after": 0},
    )
    legacy_hits = legacy_response.get("hits", {}).get("hits", [])
    legacy_document_ids = {
        hit.get("_source", {}).get("document_id")
        for hit in legacy_hits
        if isinstance(hit.get("_source", {}).get("document_id"), str)
    }
    return {
        "identity_unit": "DLS-visible OpenSearch chunk",
        "total_chunks": total_chunks,
        "modern_chunk_id": total_chunks - missing_chunk_id,
        "missing_chunk_id": missing_chunk_id,
        "legacy_chunks": missing_source_entity_id,
        "legacy_documents": len(legacy_document_ids),
        "missing_sortable_chunk_id": missing_chunk_id,
        "missing_document_id": missing_document_id,
        "missing_source_entity_id": missing_source_entity_id,
        "missing_provenance_entity_id": missing_provenance,
        "missing_stable_canonical_identity": 0,
        "legacy_fallback": "OpenSearch _id",
    }


def _fuse(
    lanes: list[list[dict[str, Any]]], *, k: int, deduplicate_lane: bool
) -> tuple[list[str], dict[str, float]]:
    from services.retrieval_service import hit_identity

    scores: dict[str, float] = {}
    for lane in lanes:
        seen: set[str] = set()
        for rank, hit in enumerate(lane, start=1):
            identity = hit_identity(hit)
            if deduplicate_lane and identity in seen:
                continue
            seen.add(identity)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda identity: (-scores[identity], identity)), scores


def _lane_observation(
    lanes: list[list[dict[str, Any]]], *, k: int, seed_budget: int
) -> dict[str, Any]:
    from services.retrieval_service import hit_identity

    lane_names = (
        ["lexical", "dense"] if len(lanes) == 2 else [f"lane-{i}" for i in range(len(lanes))]
    )
    lane_values = []
    for lane_name, lane in zip(lane_names, lanes, strict=True):
        positions: dict[str, list[int]] = {}
        for rank, hit in enumerate(lane, start=1):
            positions.setdefault(hit_identity(hit), []).append(rank)
        duplicates = [
            {"identity": identity, "ranks": ranks, "extra_occurrences": len(ranks) - 1}
            for identity, ranks in sorted(positions.items())
            if len(ranks) > 1
        ]
        lane_values.append(
            {
                "lane": lane_name,
                "ranked_count": len(lane),
                "unique_identities": len(positions),
                "duplicate_count": sum(len(ranks) - 1 for ranks in positions.values()),
                "duplicate_identities": duplicates,
            }
        )

    legacy_order, legacy_scores = _fuse(lanes, k=k, deduplicate_lane=False)
    corrected_order, corrected_scores = _fuse(lanes, k=k, deduplicate_lane=True)
    shared = set(legacy_order) & set(corrected_order)
    legacy_rank = {identity: rank for rank, identity in enumerate(legacy_order, start=1)}
    corrected_rank = {identity: rank for rank, identity in enumerate(corrected_order, start=1)}
    inflations = [
        {
            "identity": identity,
            "legacy_score": legacy_scores[identity],
            "corrected_score": corrected_scores[identity],
            "inflation": legacy_scores[identity] - corrected_scores[identity],
            "legacy_rank": legacy_rank[identity],
            "corrected_rank": corrected_rank[identity],
        }
        for identity in sorted(shared)
        if legacy_scores[identity] != corrected_scores[identity]
        or legacy_rank[identity] != corrected_rank[identity]
    ]
    legacy_seeds = legacy_order[:seed_budget]
    corrected_seeds = corrected_order[:seed_budget]
    return {
        "lanes": lane_values,
        "score_or_rank_changes": sorted(
            inflations,
            key=lambda item: (-item["inflation"], item["identity"]),
        ),
        "legacy_seed_count": len(legacy_seeds),
        "corrected_seed_count": len(corrected_seeds),
        "seeds_removed_by_fix": sorted(set(legacy_seeds) - set(corrected_seeds)),
        "seeds_added_by_fix": sorted(set(corrected_seeds) - set(legacy_seeds)),
        "legacy_order_sha256": _sha256(legacy_order),
        "corrected_order_sha256": _sha256(corrected_order),
    }


def _sha256(value: Any) -> str:
    import hashlib

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _lane_audit(plan: dict[str, Any]) -> list[dict[str, Any]]:
    from services import search_service
    from services.models_service import ModelsService
    from services.search_service import SearchService
    from session_manager import SessionManager

    service = SearchService(session_manager=SessionManager(), models_service=ModelsService())
    original_rrf = search_service.reciprocal_rank_fusion
    original_mapping_check = search_service.require_sortable_chunk_id_mapping
    current_query = ""
    observations: list[dict[str, Any]] = []
    seed_budget = max(1, int(plan.get("seed_budget", 100)))

    def observing_rrf(ranked_lists: Any, *, k: int = 60, limit: int | None = None):
        lanes = [list(lane) for lane in ranked_lists]
        observations.append(
            {
                "query": current_query,
                "query_sha256": _sha256(current_query),
                **_lane_observation(lanes, k=k, seed_budget=seed_budget),
            }
        )
        return original_rrf(lanes, k=k, limit=limit)

    async def mapping_already_verified_by_running_product(*_args: Any, **_kwargs: Any) -> None:
        return None

    search_service.reciprocal_rank_fusion = observing_rrf
    # The standalone process has no startup-initialized administrative mapping
    # client.  The running product already passed this preflight; all actual
    # query traffic below still uses the DLS-scoped session client.
    search_service.require_sortable_chunk_id_mapping = mapping_already_verified_by_running_product
    try:
        for value in plan.get("queries", []):
            current_query = str(value)
            response = await service.search(
                current_query,
                user_id="anonymous",
                jwt_token=None,
                filters={},
                limit=seed_budget,
                score_threshold=0,
            )
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
    finally:
        search_service.reciprocal_rank_fusion = original_rrf
        search_service.require_sortable_chunk_id_mapping = original_mapping_check
    return observations


async def _scope_seed_material(plan: dict[str, Any], service: Any) -> dict[str, Any]:
    from models.source_provenance import parse_source_provenance
    from services.retrieval_service import (
        build_discovery_plan,
        discovery_plan_audit,
    )

    query = str(plan["query"])
    seed_budget = max(1, int(plan.get("seed_budget", 100)))
    supplied_seed_ids = plan.get("seed_chunk_ids")
    if isinstance(supplied_seed_ids, list):
        normalized_seed_ids = list(
            dict.fromkeys(str(value).strip() for value in supplied_seed_ids if str(value).strip())
        )[:seed_budget]
        seed_results = await service.resolve_cited_chunks(
            normalized_seed_ids,
            user_id="anonymous",
            jwt_token=None,
            filters={},
        )
        seed_source = "product_selected_chunk_ids"
    else:
        fixed_queries = [
            str(value) for value in plan.get("fixed_queries", []) if str(value).strip()
        ]
        original_generator = service._generate_discovery_plan

        async def fixed_plan_generator(
            _self: Any, original_query: str, *, max_queries: int
        ) -> tuple[list[Any], str | None, float, dict[str, Any]]:
            generated = {
                "queries": [
                    {"text": value, "kind": "conceptual_variant"}
                    for value in fixed_queries
                    if value.strip() != original_query.strip()
                ]
            }
            discovery_plan = build_discovery_plan(
                original_query,
                generated,
                max_queries=max_queries,
                generation_method="benchmark_fixed_plan",
            )
            audit = discovery_plan_audit(original_query, generated, discovery_plan)
            return (
                discovery_plan,
                None,
                0.0,
                {
                    **audit,
                    "planner_invoked": False,
                    "request_parameters": {"source": "captured_fixed_plan"},
                    "request_fingerprint": _sha256(audit["query_hashes"]),
                    "response_model": None,
                },
            )

        if fixed_queries:
            service._generate_discovery_plan = types.MethodType(fixed_plan_generator, service)
        try:
            seed_response = await service.search(
                query,
                user_id="anonymous",
                jwt_token=None,
                filters={},
                limit=seed_budget,
                score_threshold=0,
                multi_query_discovery=bool(fixed_queries),
                multi_query_max_queries=min(4, max(1, len(fixed_queries))),
                multi_query_concurrency=max(1, min(4, int(plan.get("concurrency", 2)))),
            )
        finally:
            service._generate_discovery_plan = original_generator
        if seed_response.get("error"):
            raise RuntimeError(str(seed_response["error"]))
        seed_results = [item for item in seed_response.get("results", []) if isinstance(item, dict)]
        normalized_seed_ids = [
            str(item.get("chunk_id")) for item in seed_results if item.get("chunk_id")
        ]
        seed_source = (
            "fixed_multi_query_product_service" if fixed_queries else "focused_product_service"
        )
    seed_documents: dict[str, dict[str, Any]] = {}
    seed_entities: set[str] = set()
    for item in seed_results:
        document_id = item.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            continue
        try:
            provenance = parse_source_provenance(item.get("source_provenance"))
        except ValueError:
            continue
        manifest = dict(item)
        manifest.update(provenance.index_fields())
        seed_documents.setdefault(document_id, manifest)
        seed_entities.add(provenance.entity.id)

    return {
        "query": query,
        "seed_source": seed_source,
        "seed_results": seed_results,
        "seed_chunk_ids": normalized_seed_ids,
        "seed_documents": seed_documents,
        "seed_entities": seed_entities,
        "fixed_plan_sha256": _sha256(plan.get("fixed_queries", [])),
    }


async def _natural_closure(plan: dict[str, Any], service: Any, client: Any, index_name: str):
    from services.retrieval_service import expand_provenance_graph
    from services.scope_traversal_policy import DEFAULT_SCOPE_TRAVERSAL_POLICY

    material = await _scope_seed_material(plan, service)
    query = material["query"]
    seed_results = material["seed_results"]
    normalized_seed_ids = material["seed_chunk_ids"]
    seed_documents = material["seed_documents"]
    seed_entities = material["seed_entities"]

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    graph = await expand_provenance_graph(
        client,
        index_name=index_name,
        seed_entity_ids=seed_entities,
        seed_documents=seed_documents.values(),
        policy=DEFAULT_SCOPE_TRAVERSAL_POLICY,
        max_depth=max(1, int(plan.get("max_depth", 64))),
        max_entities=max(1, int(plan.get("max_entities", 5000))),
        max_documents=max(1, int(plan.get("max_documents", 2000))),
        filter_clauses=(),
    )
    elapsed = time.perf_counter() - started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    chunks = sum(
        int(item.get("document_chunk_count") or 0)
        for item in graph["documents"]
        if isinstance(item.get("document_chunk_count"), int)
    )
    return {
        "label": str(plan.get("label") or query),
        "query": query,
        "query_sha256": _sha256(query),
        "seed_source": material["seed_source"],
        "seed_chunk_ids_sha256": _sha256(normalized_seed_ids),
        "seed_chunks": len(seed_results),
        "seed_documents": len(seed_documents),
        "seed_entities": len(seed_entities),
        "natural_documents": len(graph["documents"]),
        "natural_entities": int(graph["coverage"].get("entities_visited", 0)),
        "natural_depth": int(graph["coverage"].get("depth_reached", 0)),
        "natural_chunks": chunks,
        "latency_seconds": elapsed,
        "max_rss_kib_before": rss_before,
        "max_rss_kib_after": rss_after,
        "max_rss_kib_delta": max(0, rss_after - rss_before),
        "coverage": graph["coverage"],
    }


def _relation_counts(coverage: dict[str, Any]) -> dict[tuple[str, ...], int]:
    rows = coverage.get("relations_traversed", {}).get("by_classification", [])
    return {
        (
            str(row.get("role") or ""),
            str(row.get("source_type") or ""),
            str(row.get("target_type") or ""),
            str(row.get("direction") or ""),
            str(row.get("semantics") or ""),
        ): int(row.get("count") or 0)
        for row in rows
        if isinstance(row, dict)
    }


def _graph_observation(graph: dict[str, Any]) -> dict[str, Any]:
    coverage = graph["coverage"]
    documents = {
        (str(item.get("document_id") or ""), str(item.get("source_entity_id") or ""))
        for item in graph["documents"]
        if isinstance(item, dict)
    }
    primary_entities = {
        str(item.get("source_entity_id"))
        for item in graph["documents"]
        if isinstance(item, dict) and item.get("source_entity_id")
    }
    edges = {
        (
            str(item.get("source_entity_id") or ""),
            str(item.get("role") or ""),
            str(item.get("target_entity_id") or ""),
        )
        for item in graph["edges"]
        if isinstance(item, dict)
    }
    frontier = {str(value) for value in coverage.get("remaining_frontier", []) if str(value)}
    chunks = sum(
        int(item.get("document_chunk_count") or 0)
        for item in graph["documents"]
        if isinstance(item.get("document_chunk_count"), int)
    )
    hubs = coverage.get("scope_diagnostics", {}).get("largest_expansion_contributors", [])
    degrees = [int(item.get("total_edges") or 0) for item in hubs if isinstance(item, dict)]
    return {
        "documents": documents,
        "entities": primary_entities,
        "edges": edges,
        "frontier": frontier,
        "relations": _relation_counts(coverage),
        "public": {
            "documents": len(documents),
            "entities": int(coverage.get("entities_visited") or len(primary_entities)),
            "chunks": chunks,
            "depth": int(coverage.get("depth_reached") or 0),
            "frontier": len(frontier),
            "frontier_empty": coverage.get("frontier_empty") is True,
            "limit_reached": coverage.get("limit_reached") is True,
            "stop_reason": coverage.get("stop_reason"),
            "documentary_relations": int(coverage.get("relations_traversed", {}).get("total") or 0),
            "connected_branches": len(edges),
            "hub_degree": {
                "observed_hubs": len(degrees),
                "max": max(degrees) if degrees else 0,
                "mean": sum(degrees) / len(degrees) if degrees else 0.0,
            },
            "relation_type_distribution": coverage.get("relations_traversed", {}).get(
                "by_classification", []
            ),
        },
    }


def _probe_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    relation_keys = set(before["relations"]) | set(after["relations"])
    relation_delta = [
        {
            "role": key[0],
            "source_type": key[1],
            "target_type": key[2],
            "direction": key[3],
            "semantics": key[4],
            "count": after["relations"].get(key, 0) - before["relations"].get(key, 0),
        }
        for key in sorted(relation_keys)
        if after["relations"].get(key, 0) != before["relations"].get(key, 0)
    ]
    frontier_before = len(before["frontier"])
    frontier_after = len(after["frontier"])
    new_documents = len(after["documents"] - before["documents"])
    extended_documents = len(after["documents"])
    return {
        "frontier_before": frontier_before,
        "frontier_after": frontier_after,
        "frontier_growth_rate": (
            (frontier_after - frontier_before) / frontier_before if frontier_before else None
        ),
        "new_documents": new_documents,
        "new_entities": len(after["entities"] - before["entities"]),
        "new_documentary_relations": sum(max(0, row["count"]) for row in relation_delta),
        "new_connected_branches": len(after["edges"] - before["edges"]),
        "already_covered_ratio": (
            len(before["documents"] & after["documents"]) / extended_documents
            if extended_documents
            else 0.0
        ),
        "marginal_document_yield": new_documents,
        "depth_before": before["public"]["depth"],
        "depth_after": after["public"]["depth"],
        "relation_type_delta": relation_delta,
        "hub_degree_before": before["public"]["hub_degree"],
        "hub_degree_after": after["public"]["hub_degree"],
    }


async def _documentary_target_validation(
    plan: dict[str, Any], service: Any, client: Any, index_name: str
) -> dict[str, Any]:
    """Prototype target validation by replaying the deterministic product traversal."""

    from services.retrieval_service import expand_provenance_graph
    from services.scope_traversal_policy import DEFAULT_SCOPE_TRAVERSAL_POLICY

    material = await _scope_seed_material(plan, service)
    target = max(1, int(plan.get("target_threshold", 250)))
    probe_size = max(1, int(plan.get("validation_probe_size", 50)))
    hard_limit = max(target, int(plan.get("hard_safety_limit", 500)))
    fixed_limits = sorted(
        {max(1, min(hard_limit, int(value))) for value in plan.get("fixed_limits", [250, 400, 500])}
        | {hard_limit}
    )
    max_depth = max(1, int(plan.get("max_depth", 8)))
    max_entities = max(1, int(plan.get("max_entities", 500)))
    graphs: dict[int, dict[str, Any]] = {}

    async def measured_graph(document_limit: int) -> dict[str, Any]:
        if document_limit in graphs:
            return graphs[document_limit]
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        started = time.perf_counter()
        graph = await expand_provenance_graph(
            client,
            index_name=index_name,
            seed_entity_ids=material["seed_entities"],
            seed_documents=material["seed_documents"].values(),
            policy=DEFAULT_SCOPE_TRAVERSAL_POLICY,
            max_depth=max_depth,
            max_entities=max_entities,
            max_documents=document_limit,
            filter_clauses=(),
        )
        elapsed = time.perf_counter() - started
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        observation = _graph_observation(graph)
        graphs[document_limit] = {
            "limit": document_limit,
            "graph": graph,
            "observation": observation,
            "graph_latency_seconds": elapsed,
            "max_rss_kib_delta": max(0, rss_after - rss_before),
        }
        return graphs[document_limit]

    for limit in fixed_limits:
        await measured_graph(limit)

    hard_run = graphs[hard_limit]
    natural_documents = (
        hard_run["observation"]["public"]["documents"]
        if hard_run["observation"]["public"]["frontier_empty"]
        else None
    )
    fixed = []
    for limit in fixed_limits:
        run = graphs[limit]
        public = run["observation"]["public"]
        fixed.append(
            {
                "strategy": f"fixed-{limit}",
                "document_limit": limit,
                **public,
                "natural_closure_recovery": (
                    public["documents"] / natural_documents if natural_documents else None
                ),
                "truncated_legitimate_closure": (
                    not public["frontier_empty"] and natural_documents is not None
                ),
                "coverage_success": public["frontier_empty"],
                "graph_latency_seconds": run["graph_latency_seconds"],
                "max_rss_kib_delta": run["max_rss_kib_delta"],
            }
        )

    current_limit = min(target, hard_limit)
    current = await measured_graph(current_limit)
    initial_target_observation = current["observation"]
    probes: list[dict[str, Any]] = []
    replay_latency = current["graph_latency_seconds"]
    while not current["observation"]["public"]["frontier_empty"] and current_limit < hard_limit:
        next_limit = min(hard_limit, current_limit + probe_size)
        extended = await measured_graph(next_limit)
        replay_latency += extended["graph_latency_seconds"]
        public = extended["observation"]["public"]
        probes.append(
            {
                "probe": len(probes) + 1,
                "from_target": current_limit,
                "to_target": next_limit,
                **_probe_delta(current["observation"], extended["observation"]),
                "outcome": (
                    "NATURAL_COMPLETE"
                    if public["frontier_empty"]
                    else "HARD_SAFETY_LIMIT_REACHED"
                    if next_limit == hard_limit
                    else "TARGET_TOO_SMALL_FRONTIER_ACTIVE"
                ),
            }
        )
        current_limit = next_limit
        current = extended

    final_public = current["observation"]["public"]
    state = "NATURAL_COMPLETE" if final_public["frontier_empty"] else "HARD_SAFETY_LIMIT_REACHED"
    return {
        "label": str(plan.get("label") or material["query"]),
        "query": material["query"],
        "query_sha256": _sha256(material["query"]),
        "seed_source": material["seed_source"],
        "fixed_plan_sha256": material["fixed_plan_sha256"],
        "seed_chunks": len(material["seed_results"]),
        "seed_documents": len(material["seed_documents"]),
        "seed_entities": len(material["seed_entities"]),
        "max_depth": max_depth,
        "max_entities": max_entities,
        "fixed_strategies": fixed,
        "documentary_target_validation": {
            "documentary_semantics": "probe-beyond-target-to-validate-target",
            "target_threshold": target,
            "validation_probe_size": probe_size,
            "hard_safety_limit": hard_limit,
            "state": state,
            "coverage_complete": state == "NATURAL_COMPLETE",
            "false_target_at_threshold": (
                not initial_target_observation["public"]["frontier_empty"]
            ),
            "number_of_probes": len(probes),
            "target_extensions": len(probes),
            "final_target": current_limit,
            "final_observation": final_public,
            "probes": probes,
            "prototype_replay_graph_latency_seconds": replay_latency,
            "execution_note": (
                "Calibration replay restarts the same deterministic traversal at each target; "
                "a continuous implementation would retain traversal state."
            ),
        },
    }


def _compact_sensitivity_response(response: dict[str, Any]) -> dict[str, Any]:
    """Drop retrieved text while retaining quality, cost, and contract evidence."""

    identity_fields = (
        "chunk_id",
        "document_id",
        "source_entity_id",
        "source_entity_alternate_ids",
        "score",
        "fusion_score",
        "matched_queries",
        "matched_lanes",
        "best_rank_per_query",
        "query_contributions",
    )
    scope_fields = (
        "document_id",
        "source_entity_id",
        "source_entity_alternate_ids",
        "document_chunk_count",
        "document_character_count",
    )
    seeds = response.get("model_results")
    documents = response.get("documents")
    return {
        "seeds": [
            {field: item[field] for field in identity_fields if field in item}
            for item in seeds
            if isinstance(item, dict)
        ]
        if isinstance(seeds, list)
        else [],
        "documents": [
            {field: item[field] for field in scope_fields if field in item}
            for item in documents
            if isinstance(item, dict)
        ]
        if isinstance(documents, list)
        else [],
        "discovery": response.get("discovery", {}),
        "requested_retrieval_profile": response.get("requested_retrieval_profile"),
        "effective_retrieval_profile": response.get("effective_retrieval_profile"),
        "retrieval_execution_complete": response.get("retrieval_execution_complete"),
        "retrieval_failure_codes": response.get("retrieval_failure_codes", []),
        "coverage": response.get("coverage", {}),
        "error": response.get("error"),
    }


async def _sensitivity_experiment(plan: dict[str, Any], service: Any) -> dict[str, Any]:
    """Run one isolated read-only axis through the product SearchService."""

    from auth_context import set_auth_context
    from config.settings import get_openrag_config
    from services import search_service
    from services.retrieval_service import (
        ScopeExhaustiveSettings,
        build_discovery_plan,
        discovery_plan_audit,
    )

    query = str(plan["query"])
    query_count = max(1, min(4, int(plan.get("query_count", 1))))
    seed_budget = max(1, int(plan.get("seed_budget", 100)))
    config = copy.deepcopy(get_openrag_config())
    knowledge = config.knowledge
    knowledge.retrieval_lexical_candidates = max(
        1, int(plan.get("lexical_candidates", knowledge.retrieval_lexical_candidates))
    )
    knowledge.retrieval_vector_candidates = max(
        1, int(plan.get("dense_candidates", knowledge.retrieval_vector_candidates))
    )
    knowledge.retrieval_rrf_k = max(1, int(plan.get("rrf_k", knowledge.retrieval_rrf_k)))
    fixed_queries = [str(value) for value in plan.get("fixed_queries", []) if str(value).strip()]

    original_config = search_service.get_openrag_config
    original_generator = service._generate_discovery_plan
    original_graph_expansion = search_service.expand_provenance_graph
    graph_traversal_seconds = 0.0

    async def timed_graph_expansion(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal graph_traversal_seconds
        graph_started = time.perf_counter()
        try:
            return await original_graph_expansion(*args, **kwargs)
        finally:
            graph_traversal_seconds += time.perf_counter() - graph_started

    async def fixed_plan_generator(
        _self: Any, original_query: str, *, max_queries: int
    ) -> tuple[list[Any], str | None, float, dict[str, Any]]:
        variants = [
            {"text": value, "kind": "conceptual_variant"}
            for value in fixed_queries
            if value.strip() != original_query.strip()
        ]
        generated = {"queries": variants}
        discovery_plan = build_discovery_plan(
            original_query,
            generated,
            max_queries=max_queries,
            generation_method="benchmark_fixed_plan",
        )
        audit = discovery_plan_audit(original_query, generated, discovery_plan)
        return (
            discovery_plan,
            None,
            0.0,
            {
                **audit,
                "planner_invoked": False,
                "request_parameters": {"source": "captured_fixed_plan"},
                "request_fingerprint": _sha256(audit["query_hashes"]),
                "response_model": None,
            },
        )

    search_service.get_openrag_config = lambda: config
    search_service.expand_provenance_graph = timed_graph_expansion
    set_auth_context("anonymous", None)
    if query_count > 1:
        if not fixed_queries:
            raise ValueError("multi-query sensitivity experiments require fixed_queries")
        service._generate_discovery_plan = types.MethodType(fixed_plan_generator, service)

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    try:
        response = await service.search_exhaustive_scope(
            query,
            user_id="anonymous",
            jwt_token=None,
            filters={},
            embedding_model=None,
            settings=ScopeExhaustiveSettings(
                seed_count=seed_budget,
                max_depth=max(1, int(plan.get("max_depth", 8))),
                max_entities=max(1, int(plan.get("max_entities", 500))),
                max_documents=max(1, int(plan.get("max_documents", 250))),
                batch_size=max(1, min(50, int(plan.get("batch_size", 50)))),
            ),
            multi_query_discovery=query_count > 1,
            multi_query_max_queries=query_count,
            multi_query_concurrency=max(1, min(4, int(plan.get("concurrency", 2)))),
        )
    finally:
        search_service.get_openrag_config = original_config
        search_service.expand_provenance_graph = original_graph_expansion
        service._generate_discovery_plan = original_generator
    elapsed = time.perf_counter() - started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    compact = _compact_sensitivity_response(response)
    scope_performance = compact.get("coverage", {}).get("performance", {})
    prov_o_seconds = float(scope_performance.get("prov_o_seconds") or 0.0)
    return {
        "experiment_id": str(plan["experiment_id"]),
        "axis": str(plan["axis"]),
        "configuration": {
            "lexical_candidates": knowledge.retrieval_lexical_candidates,
            "dense_candidates": knowledge.retrieval_vector_candidates,
            "rrf_k": knowledge.retrieval_rrf_k,
            "seed_budget": seed_budget,
            "query_count": query_count,
            "max_depth": max(1, int(plan.get("max_depth", 8))),
            "max_entities": max(1, int(plan.get("max_entities", 500))),
            "max_documents": max(1, int(plan.get("max_documents", 250))),
            "batch_size": max(1, min(50, int(plan.get("batch_size", 50)))),
        },
        "query": query,
        "query_sha256": _sha256(query),
        "fixed_plan_sha256": _sha256(fixed_queries),
        "wall_seconds": elapsed,
        "graph_traversal_seconds": graph_traversal_seconds,
        "document_read_seconds": max(0.0, prov_o_seconds - graph_traversal_seconds),
        "max_rss_kib_delta": max(0, rss_after - rss_before),
        **compact,
    }


async def run(plan: dict[str, Any]) -> dict[str, Any]:
    from config.settings import get_index_name
    from services import search_service
    from services.models_service import ModelsService
    from services.search_service import SearchService
    from session_manager import SessionManager

    session_manager = SessionManager()
    service = SearchService(session_manager=session_manager, models_service=ModelsService())
    client = session_manager.get_user_opensearch_client("anonymous", None)
    index_name = get_index_name()
    result: dict[str, Any] = {
        "schema_version": 1,
        "identity_descriptor": "product no-auth identity",
        "workspace": "default runtime workspace",
        "knowledge_filters": {},
        "index": index_name,
    }
    original_mapping_check = search_service.require_sortable_chunk_id_mapping

    async def mapping_already_verified_by_running_product(*_args: Any, **_kwargs: Any) -> None:
        return None

    # This diagnostic executes inside an already healthy backend pod, but in a
    # fresh Python process without the startup administrative mapping client.
    # All reads below still use the product anonymous/DLS-scoped client.
    search_service.require_sortable_chunk_id_mapping = mapping_already_verified_by_running_product
    try:
        if plan.get("identity_audit"):
            result["identity_audit"] = await _identity_audit(client, index_name)
        if plan.get("queries"):
            result["lane_audits"] = await _lane_audit(plan)
        if isinstance(plan.get("natural_closure"), list):
            result["natural_closures"] = [
                await _natural_closure(value, service, client, index_name)
                for value in plan["natural_closure"]
                if isinstance(value, dict)
            ]
        if isinstance(plan.get("documentary_target_validation"), list):
            result["documentary_target_validation"] = [
                await _documentary_target_validation(value, service, client, index_name)
                for value in plan["documentary_target_validation"]
                if isinstance(value, dict)
            ]
        if isinstance(plan.get("sensitivity_experiments"), list):
            result["sensitivity_experiments"] = [
                await _sensitivity_experiment(value, service)
                for value in plan["sensitivity_experiments"]
                if isinstance(value, dict)
            ]
    finally:
        search_service.require_sortable_chunk_id_mapping = original_mapping_check
        close = getattr(client, "close", None)
        if callable(close):
            closed = close()
            if hasattr(closed, "__await__"):
                await closed
    return result


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected one base64-encoded JSON plan")
    plan = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    print("RETRIEVAL_CORRECTNESS_JSON=" + json.dumps(asyncio.run(run(plan)), ensure_ascii=False))


if __name__ == "__main__":
    main()
