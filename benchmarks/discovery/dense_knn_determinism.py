"""Bounded, read-only dense KNN reproducibility capture.

Run this module inside the backend runtime so it can reuse the configured
embedding provider and the DLS-scoped OpenSearch client.  A base64-encoded JSON
plan supplies generic queries and candidate strategies; no corpus vocabulary is
embedded in the harness.

The capture intentionally separates ANN membership, rank, and score stability.
Segment snapshots are deduplicated by fingerprint while every run records the
snapshot it observed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import statistics
import sys
import time
from typing import Any

SOURCE_FIELDS = [
    "chunk_id",
    "document_id",
    "source_entity_id",
    "embedding_model",
]


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare every observation with the first byte-identical request."""
    if not runs:
        return {}
    reference = runs[0]["hits"]
    reference_ids = [hit["chunk_id"] for hit in reference]
    reference_membership = set(reference_ids)
    reference_ranks = {identity: rank for rank, identity in enumerate(reference_ids, start=1)}
    reference_scores = {hit["chunk_id"]: hit["score"] for hit in reference}
    ordered_hashes: set[str] = set()
    membership_hashes: set[str] = set()
    jaccards: list[float] = []
    rank_correlations: list[float] = []
    max_rank_displacement = 0
    score_changes = 0
    max_score_drift = 0.0

    for run in runs:
        identities = [hit["chunk_id"] for hit in run["hits"]]
        membership = set(identities)
        ordered_hashes.add(canonical_sha256(identities))
        membership_hashes.add(canonical_sha256(sorted(membership)))
        union = reference_membership | membership
        jaccards.append(len(reference_membership & membership) / len(union) if union else 1.0)
        ranks = {identity: rank for rank, identity in enumerate(identities, start=1)}
        common = sorted(reference_membership & membership)
        if common:
            displacements = [abs(reference_ranks[item] - ranks[item]) for item in common]
            max_rank_displacement = max(max_rank_displacement, max(displacements))
            if len(common) == 1:
                rank_correlations.append(1.0)
            else:
                numerator = 6 * sum(
                    (reference_ranks[item] - ranks[item]) ** 2 for item in common
                )
                denominator = len(common) * (len(common) ** 2 - 1)
                rank_correlations.append(1.0 - numerator / denominator)
        for hit in run["hits"]:
            identity = hit["chunk_id"]
            if identity not in reference_scores:
                continue
            drift = abs(float(hit["score"]) - float(reference_scores[identity]))
            if drift != 0:
                score_changes += 1
                max_score_drift = max(max_score_drift, drift)

    latencies = [float(run["wall_latency_ms"]) for run in runs]
    result = {
        "runs": len(runs),
        "distinct_request_fingerprints": len(
            {run["request_fingerprint"] for run in runs}
        ),
        "distinct_query_vector_hashes": len({run["query_vector_sha256"] for run in runs}),
        "distinct_index_uuids": len({run["index_uuid"] for run in runs}),
        "distinct_segment_snapshots": len({run["segment_snapshot_id"] for run in runs}),
        "distinct_ordered_result_sets": len(ordered_hashes),
        "distinct_membership_sets": len(membership_hashes),
        "membership_jaccard_min": min(jaccards),
        "membership_jaccard_mean": statistics.fmean(jaccards),
        "rank_correlation_min": min(rank_correlations) if rank_correlations else None,
        "max_rank_displacement": max_rank_displacement,
        "score_changes_vs_reference": score_changes,
        "max_absolute_score_drift": max_score_drift,
        "latency_ms": {
            "min": min(latencies),
            "mean": statistics.fmean(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies),
        },
    }
    return result


def _dense_body(
    *,
    vector_field: str,
    vector: list[float],
    k: int,
    overquery_factor: int | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"vector": vector, "k": k}
    if overquery_factor is not None:
        query["method_parameters"] = {"overquery_factor": overquery_factor}
    return {
        "query": {
            "bool": {
                "should": [
                    {
                        "dis_max": {
                            "tie_breaker": 0.0,
                            "queries": [{"knn": {vector_field: query}}],
                        }
                    }
                ],
                "minimum_should_match": 1,
                "filter": [{"exists": {"field": vector_field}}],
            }
        },
        "_source": SOURCE_FIELDS,
        "size": k,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
    }


def _segment_payload(raw: dict[str, Any], index_name: str) -> dict[str, Any]:
    index = raw.get("indices", {}).get(index_name, {})
    shards: list[dict[str, Any]] = []
    for shard_id, copies in sorted(index.get("shards", {}).items()):
        for copy in copies:
            routing = copy.get("routing", {})
            segments = copy.get("segments", {})
            shards.append(
                {
                    "shard": str(shard_id),
                    "primary": routing.get("primary"),
                    "state": routing.get("state"),
                    "node": routing.get("node"),
                    "segments": [
                        {
                            "name": name,
                            "generation": value.get("generation"),
                            "num_docs": value.get("num_docs"),
                            "deleted_docs": value.get("deleted_docs"),
                            "size_in_bytes": value.get("size_in_bytes"),
                            "committed": value.get("committed"),
                            "search": value.get("search"),
                            "version": value.get("version"),
                            "compound": value.get("compound"),
                        }
                        for name, value in sorted(segments.items())
                    ],
                }
            )
    return {"shards": shards}


async def run(plan: dict[str, Any]) -> dict[str, Any]:
    """Execute the plan with one embedding call per query and read-only searches."""
    from config.settings import clients, get_embedding_model, get_index_name, get_openrag_config
    from services.models_service import ModelsService
    from session_manager import SessionManager
    from utils.embedding_fields import get_embedding_field_name

    index_name = get_index_name()
    search_client = SessionManager().get_user_opensearch_client("anonymous", None)
    admin_client = clients.create_index_admin_opensearch_client()
    if admin_client is None:
        raise RuntimeError("KNN audit requires a read-only index metadata client")
    configured_model = get_embedding_model() or "text-embedding-3-large"
    embedding_model = str(plan.get("embedding_model") or configured_model)
    provider = str(get_openrag_config().knowledge.embedding_provider)
    formatted_model = await ModelsService().get_litellm_model_name(
        embedding_model, provider=provider
    )
    vector_field = get_embedding_field_name(embedding_model)
    k = max(1, min(10_000, int(plan.get("k", 50))))
    index_definition = await admin_client.indices.get(index=index_name)
    index_settings = index_definition[index_name]["settings"]["index"]
    index_uuid = str(index_settings["uuid"])
    cluster_info = await admin_client.info()
    health = await admin_client.cluster.health(index=index_name)
    segment_snapshots: dict[str, dict[str, Any]] = {}
    captures: list[dict[str, Any]] = []

    async def snapshot_segments() -> str:
        payload = _segment_payload(
            await admin_client.indices.segments(index=index_name), index_name
        )
        fingerprint = canonical_sha256(payload)
        segment_snapshots.setdefault(fingerprint, payload)
        return fingerprint

    for query_spec in plan.get("queries", []):
        query_id = str(query_spec["query_id"])
        query_text = str(query_spec["text"])
        embedded = await clients.patched_embedding_client.embeddings.create(
            model=formatted_model,
            input=[query_text],
        )
        vector = getattr(embedded.data[0], "embedding", None)
        if vector is None:
            vector = embedded.data[0]["embedding"]
        vector = [float(value) for value in vector]
        vector_sha256 = canonical_sha256(vector)
        for strategy in plan.get("strategies", []):
            strategy_id = str(strategy["strategy_id"])
            repetitions = max(1, min(100, int(strategy.get("runs", 1))))
            raw_factor = strategy.get("overquery_factor")
            factor = int(raw_factor) if raw_factor is not None else None
            body = _dense_body(
                vector_field=vector_field,
                vector=vector,
                k=k,
                overquery_factor=factor,
            )
            request_fingerprint = canonical_sha256(body)
            strategy_runs: list[dict[str, Any]] = []
            for repetition in range(1, repetitions + 1):
                segment_snapshot_id = await snapshot_segments()
                started = time.perf_counter()
                response = await search_client.search(
                    index=index_name,
                    body=body,
                    params={"terminate_after": 0},
                )
                wall_latency_ms = (time.perf_counter() - started) * 1_000
                hits = []
                for rank, hit in enumerate(response.get("hits", {}).get("hits", []), start=1):
                    source = hit.get("_source", {})
                    hits.append(
                        {
                            "rank": rank,
                            "chunk_id": str(source.get("chunk_id") or hit.get("_id") or ""),
                            "document_id": source.get("document_id"),
                            "source_entity_id": source.get("source_entity_id"),
                            "score": hit.get("_score"),
                        }
                    )
                capture = {
                    "query_id": query_id,
                    "strategy_id": strategy_id,
                    "repetition": repetition,
                    "request_fingerprint": request_fingerprint,
                    "query_vector_sha256": vector_sha256,
                    "index_uuid": index_uuid,
                    "segment_snapshot_id": segment_snapshot_id,
                    "opensearch_took_ms": response.get("took"),
                    "wall_latency_ms": wall_latency_ms,
                    "hits": hits,
                }
                captures.append(capture)
                strategy_runs.append(capture)
            strategy["summary_by_query"] = strategy.get("summary_by_query", {})
            strategy["summary_by_query"][query_id] = summarize_runs(strategy_runs)

    final_segment_snapshot_id = await snapshot_segments()
    result = {
        "schema_version": 1,
        "contract_subject": "dense-knn-reproducibility",
        "index": {
            "name": index_name,
            "uuid": index_uuid,
            "settings": index_settings,
            "mapping": index_definition[index_name]["mappings"],
            "health": health,
        },
        "opensearch": {
            "version": cluster_info.get("version"),
            "cluster_name": cluster_info.get("cluster_name"),
        },
        "embedding": {
            "provider": provider,
            "model": embedding_model,
            "field": vector_field,
        },
        "k": k,
        "queries": plan.get("queries", []),
        "strategies": plan.get("strategies", []),
        "segment_snapshots": segment_snapshots,
        "final_segment_snapshot_id": final_segment_snapshot_id,
        "captures": captures,
    }
    await search_client.close()
    await admin_client.close()
    return result


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected one base64-encoded JSON plan")
    plan = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    result = asyncio.run(run(plan))
    print("KNN_AUDIT_RESULT_JSON=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
