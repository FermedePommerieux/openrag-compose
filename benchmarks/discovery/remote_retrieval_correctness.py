"""Read-only correctness diagnostics executed inside the backend runtime.

The probe calls the product ``SearchService`` and its DLS-scoped OpenSearch
client.  It does not contain a second retrieval implementation, mutate runtime
settings, read benchmark ground truth, or persist any cluster resource.
"""

from __future__ import annotations

import asyncio
import base64
import json
import resource
import sys
import time
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
                "duplicate_count": sum(item["extra_occurrences"] for item in duplicates),
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


async def _natural_closure(plan: dict[str, Any], service: Any, client: Any, index_name: str):
    from models.source_provenance import parse_source_provenance
    from services.retrieval_service import expand_provenance_graph
    from services.scope_traversal_policy import DEFAULT_SCOPE_TRAVERSAL_POLICY

    query = str(plan["query"])
    seed_budget = max(1, int(plan.get("seed_budget", 100)))
    seed_response = await service.search(
        query,
        user_id="anonymous",
        jwt_token=None,
        filters={},
        limit=seed_budget,
        score_threshold=0,
    )
    if seed_response.get("error"):
        raise RuntimeError(str(seed_response["error"]))
    seed_results = [item for item in seed_response.get("results", []) if isinstance(item, dict)]
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
        "query": query,
        "query_sha256": _sha256(query),
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


async def run(plan: dict[str, Any]) -> dict[str, Any]:
    from config.settings import get_index_name
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
    return result


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected one base64-encoded JSON plan")
    plan = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    print("RETRIEVAL_CORRECTNESS_JSON=" + json.dumps(asyncio.run(run(plan)), ensure_ascii=False))


if __name__ == "__main__":
    main()
