"""Capture and evaluate discovery through the deployed product search endpoint.

This harness deliberately owns no planner, retrieval, fusion, graph traversal,
or certification logic.  It sends versioned benchmark cases to ``/api/search``
and records a compact, text-free projection of the product response.  Human
ground truth is loaded only by the local evaluator after capture.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any
from urllib.request import Request, urlopen

from benchmarks.discovery.corpus import corpus_changed
from benchmarks.discovery.final_baseline import _view_metrics, _write_json
from benchmarks.discovery.ground_truth import load_ground_truth
from services.retrieval_service import verify_scope_coverage_certificate

_RATIO_METRICS = (
    "seed_document_recall",
    "seed_component_recall",
    "post_prov_o_document_recall",
    "post_prov_o_component_recall",
    "precision",
)
_SCALAR_METRICS = (
    "expansion_per_seed_document",
    "total_latency_seconds",
)
_SEED_FIELDS = (
    "chunk_id",
    "document_id",
    "source_entity_id",
    "occurrence_id",
    "filename",
    "score",
    "fusion_score",
    "rrf_rank",
    "matched_queries",
    "matched_lanes",
    "best_rank_per_query",
    "query_contributions",
)
_SCOPE_FIELDS = (
    "document_id",
    "source_entity_id",
    "occurrence_id",
    "filename",
    "connector_type",
    "source_entity_type",
    "source_entity_system",
    "complete",
    "status_code",
    "error",
)
_COVERAGE_FIELDS = (
    "mode",
    "query",
    "scope_policy_id",
    "scope_policy_version",
    "seed_discovery_complete",
    "seed_documents",
    "seed_entities",
    "valid_provenance_seed_documents",
    "invalid_provenance_seed_documents",
    "seed_provenance_complete",
    "requested_retrieval_profile",
    "effective_retrieval_profile",
    "retrieval_execution_complete",
    "retrieval_failure_codes",
    "graph_entities_visited",
    "graph_frontier_empty",
    "graph_limit_reached",
    "graph_stop_reason",
    "graph_failed",
    "graph_error",
    "graph_stability_verified",
    "graph_stability_observations",
    "relations_unclassified",
    "identity_shared_aliases_resolved",
    "scope_diagnostics",
    "documents_discovered",
    "documents_complete",
    "documents_incomplete",
    "covered_chunks",
    "total_chunks",
    "document_read_coverage_ratio",
    "coverage_ratio",
    "complete",
    "status_code",
    "status_message",
    "failure_codes",
    "stop_reason",
    "model_evidence_chunks",
    "artifact_chunks",
    "performance",
    "certification",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validation_check(
    check_id: str, *, observed: Any, expected: Any, evidence: Any | None = None
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "PASS" if observed == expected else "FAIL",
        "observed": observed,
        "expected": expected,
        "evidence_sha256": _canonical_sha256(
            evidence if evidence is not None else {"observed": observed, "expected": expected}
        ),
    }


def _canonical_query(definition: dict[str, Any]) -> str:
    query = next(item for item in definition["queries"] if item.get("kind") == "canonical_literal")
    return str(query["text"])


def _json_object(raw: str, *, label: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _repetition_plan(args: argparse.Namespace) -> dict[int, int]:
    query_counts = sorted(set(args.query_counts))
    plan = {query_count: args.repetitions for query_count in query_counts}
    if not args.repetition_plan_json:
        return plan
    requested = _json_object(args.repetition_plan_json, label="repetition plan")
    parsed: dict[int, int] = {}
    for raw_query_count, raw_repetitions in requested.items():
        query_count = int(raw_query_count)
        repetitions = int(raw_repetitions)
        if query_count not in query_counts:
            raise ValueError(f"repetition plan contains unselected q{query_count}")
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        parsed[query_count] = repetitions
    missing = set(query_counts) - set(parsed)
    if missing:
        raise ValueError(f"repetition plan is missing query counts: {sorted(missing)}")
    return parsed


def _headers(authorization_env: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if authorization_env:
        token = os.environ.get(authorization_env, "").strip()
        if not token:
            raise ValueError(f"authorization environment variable is empty: {authorization_env}")
        headers["Authorization"] = (
            token if token.casefold().startswith("bearer ") else f"Bearer {token}"
        )
    return headers


def _get_json(url: str, *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request_headers = {key: value for key, value in headers.items() if key != "Content-Type"}
    request = Request(url, headers=request_headers)  # noqa: S310 - operator URL
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator URL
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("product API response must be an object")
    return value


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    request = Request(  # noqa: S310 - operator URL
        url,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator URL
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("product search response must be an object")
    return value


def snapshot_product_corpus(
    base_url: str,
    *,
    headers: dict[str, str],
    page_size: int,
    timeout: int,
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Snapshot the exact DLS-visible occurrence set through ``/api/files``."""

    from urllib.parse import urlencode

    records: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1
    cursor: str | None = None
    while True:
        query: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "sort_by": "filename",
            "sort_order": "asc",
        }
        if cursor:
            query["cursor"] = cursor
        url = f"{base_url.rstrip('/')}/api/files?{urlencode(query)}"
        payload = get_json(url) if get_json else _get_json(url, headers=headers, timeout=timeout)
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        total = int(payload.get("total", 0))
        expected_total = total if expected_total is None else expected_total
        last_page = page
        next_cursor = None
        for block in [payload, *payload.get("prefetched_pages", [])]:
            block_files = block.get("files", [])
            if isinstance(block_files, list):
                records.extend(item for item in block_files if isinstance(item, dict))
            last_page = int(block.get("page", last_page))
            next_cursor = block.get("next_cursor")
        if len(records) >= total or not next_cursor:
            break
        page = last_page + 1
        cursor = str(next_cursor)

    occurrence_ids = sorted(
        str(item.get("source_entity_id") or item.get("document_id") or "") for item in records
    )
    occurrence_ids = [value for value in occurrence_ids if value]
    return {
        "captured_at": _now(),
        "visible_occurrences": expected_total or 0,
        "enumerated_occurrences": len(records),
        "distinct_document_ids": len(
            {str(item["document_id"]) for item in records if item.get("document_id")}
        ),
        "distinct_source_entity_ids": len(
            {str(item["source_entity_id"]) for item in records if item.get("source_entity_id")}
        ),
        "sources": sorted(
            {
                str(item.get("source_entity_system") or item.get("connector_type"))
                for item in records
                if item.get("source_entity_system") or item.get("connector_type")
            }
        ),
        "occurrence_identity_sha256": hashlib.sha256(
            "\n".join(occurrence_ids).encode()
        ).hexdigest(),
        "complete": len(records) == (expected_total or 0),
    }


def _request_body(
    *,
    query: str,
    filters: dict[str, Any],
    query_count: int,
    seed_budget: int,
    multi_query_concurrency: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "filters": filters,
        "limit": seed_budget,
        "scoreThreshold": 0,
        "evidenceMode": "scope_exhaustive",
        "responseProfile": "default",
    }
    if query_count > 1:
        body.update(
            {
                "multiQueryDiscovery": True,
                "multiQueryMaxQueries": query_count,
                "multiQueryConcurrency": multi_query_concurrency,
            }
        )
    return body


def _copy_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: value[field] for field in fields if field in value}


def _compact_seeds(response: dict[str, Any]) -> list[dict[str, Any]]:
    seeds = response.get("model_results")
    if not isinstance(seeds, list):
        raise ValueError("scope response is missing product-selected model_results seeds")
    compact: list[dict[str, Any]] = []
    for rank, item in enumerate(seeds, start=1):
        if isinstance(item, dict):
            compact.append({"seed_rank": rank, **_copy_fields(item, _SEED_FIELDS)})
    return compact


def _compact_scope(response: dict[str, Any]) -> list[dict[str, Any]]:
    documents = response.get("documents")
    if not isinstance(documents, list):
        raise ValueError("scope response is missing documents")
    return [_copy_fields(item, _SCOPE_FIELDS) for item in documents if isinstance(item, dict)]


def _compact_coverage(response: dict[str, Any]) -> dict[str, Any]:
    coverage = response.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("scope response is missing its coverage certificate")
    return _copy_fields(coverage, _COVERAGE_FIELDS)


def _generated_queries(response: dict[str, Any], query: str) -> list[dict[str, Any]]:
    discovery = response.get("discovery")
    if isinstance(discovery, dict) and isinstance(discovery.get("queries"), list):
        return [item for item in discovery["queries"] if isinstance(item, dict)]
    return [
        {
            "query_id": "q0",
            "query_text": query,
            "query_type": "original",
            "parent_query": query,
            "generation_method": "user",
        }
    ]


def _contract_assessment(run: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    coverage = run["coverage"]
    canonical_assessment = verify_scope_coverage_certificate(coverage)
    failures.extend(
        f"canonical_certifier:{code}"
        for code in canonical_assessment["failure_codes"]
    )
    query_count = int(run["configuration"]["query_count"])
    discovery = run["discovery"]
    requested = run.get("requested_retrieval_profile")
    effective = run.get("effective_retrieval_profile")
    if not isinstance(requested, dict) or not isinstance(effective, dict):
        failures.append("retrieval_profile_missing")
        requested_lanes: dict[str, Any] = {}
        effective_lanes: dict[str, Any] = {}
    else:
        requested_lanes = requested.get("lanes", {})
        effective_lanes = effective.get("lanes", {})
        if not isinstance(requested_lanes, dict) or not isinstance(effective_lanes, dict):
            failures.append("retrieval_lane_profile_invalid")
            requested_lanes = {}
            effective_lanes = {}
    for lane, requirement in requested_lanes.items():
        lane_execution = effective_lanes.get(lane)
        if requirement == "required" and (
            not isinstance(lane_execution, dict) or lane_execution.get("status") != "succeeded"
        ):
            failures.append(f"required_lane_not_succeeded:{lane}")
    if run.get("error"):
        failures.append("product_error")
    if run.get("retrieval_execution_complete") is not True:
        failures.append("retrieval_execution_incomplete")
    if run.get("retrieval_failure_codes") not in (None, []):
        failures.append("retrieval_failure_codes_present")
    if coverage.get("retrieval_execution_complete") is not True:
        failures.append("coverage_retrieval_execution_incomplete")
    if coverage.get("requested_retrieval_profile") != requested:
        failures.append("coverage_requested_profile_mismatch")
    if coverage.get("effective_retrieval_profile") != effective:
        failures.append("coverage_effective_profile_mismatch")
    if coverage.get("complete") is not True:
        failures.append("coverage_incomplete")
    if coverage.get("status_code") != "complete":
        failures.append("coverage_status_not_complete")
    if coverage.get("failure_codes") not in (None, []):
        failures.append("coverage_failure_codes_present")
    if coverage.get("documents_complete") != coverage.get("documents_discovered"):
        failures.append("document_counter_mismatch")
    if coverage.get("covered_chunks") != coverage.get("total_chunks"):
        failures.append("chunk_counter_mismatch")
    if coverage.get("graph_frontier_empty") is not True:
        failures.append("graph_frontier_not_empty")
    if coverage.get("graph_limit_reached") is not False:
        failures.append("graph_limit_reached")
    if len(run["final_seeds"]) > int(run["configuration"]["seed_budget"]):
        failures.append("global_seed_budget_exceeded")
    if query_count > 1:
        if discovery.get("multi_query_requested") is not True:
            failures.append("multi_query_not_requested")
        if discovery.get("multi_query_executed") is not True:
            failures.append("multi_query_not_executed")
        if discovery.get("multi_query_status") != "success":
            failures.append("multi_query_not_successful")
        if len(run["generated_queries"]) < 2:
            failures.append("multi_query_has_no_generated_variant")
        if len(run["generated_queries"]) > query_count:
            failures.append("planner_query_limit_exceeded")
        if discovery.get("multi_query_query_count") != len(run["generated_queries"]):
            failures.append("planner_query_count_mismatch")
        if discovery.get("final_seed_chunk_budget") != run["configuration"]["seed_budget"]:
            failures.append("reported_seed_budget_mismatch")
    elif len(run["generated_queries"]) != 1:
        failures.append("q1_not_single_query")
    checks = [
        _validation_check(
            "product_path_endpoint",
            observed=run.get("product_endpoint"),
            expected="/api/search",
        ),
        _validation_check(
            "runtime_behavior_match",
            observed=run.get("runtime_behavior_profile", {}).get("status"),
            expected="MATCH",
        ),
        _validation_check(
            "runtime_behavior_fingerprint_stable",
            observed=run.get("runtime_behavior_fingerprint"),
            expected=run.get("expected_runtime_behavior_fingerprint"),
        ),
        _validation_check(
            "retrieval_execution_complete",
            observed=run.get("retrieval_execution_complete"),
            expected=True,
        ),
        _validation_check(
            "coverage_complete",
            observed={
                "complete": coverage.get("complete"),
                "status_code": coverage.get("status_code"),
            },
            expected={"complete": True, "status_code": "complete"},
        ),
        _validation_check(
            "global_seed_budget",
            observed=len(run["final_seeds"]) <= int(run["configuration"]["seed_budget"]),
            expected=True,
            evidence={
                "observed_seed_count": len(run["final_seeds"]),
                "seed_budget": run["configuration"]["seed_budget"],
            },
        ),
    ]
    failed_checks = [item["check_id"] for item in checks if item["status"] != "PASS"]
    failures.extend(f"validation_failed:{check_id}" for check_id in failed_checks)
    return {
        "valid": not failures,
        "failure_codes": sorted(set(failures)),
        "validation_evidence": checks,
    }


def compact_product_response(
    response: dict[str, Any],
    *,
    query: str,
    query_count: int,
    repetition: int,
    seed_budget: int,
    started_at: str,
    http_wall_seconds: float,
    runtime_profile: dict[str, Any],
    expected_runtime_behavior_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Remove chunk text while retaining identities, ranks, and contracts."""

    discovery = response.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    coverage = _compact_coverage(response)
    performance = coverage.get("performance")
    performance = performance if isinstance(performance, dict) else {}
    generated_queries = _generated_queries(response, query)
    discovery_planner = discovery.get("planner")
    planner = (
        discovery_planner
        if isinstance(discovery_planner, dict)
        else {
            "provider": runtime_profile.get("planner", {}).get("effective_provider"),
            "model": runtime_profile.get("planner", {}).get("effective_model"),
            "capability_profile": runtime_profile.get("planner", {}).get("capability_profile"),
        }
    )
    server_query_hashes = discovery.get("query_hashes")
    query_hashes = (
        [str(value) for value in server_query_hashes]
        if isinstance(server_query_hashes, list)
        else [
            _canonical_sha256(
                {
                    "query_id": item.get("query_id"),
                    "query_text": item.get("query_text"),
                    "query_type": item.get("query_type"),
                }
            )
            for item in generated_queries
        ]
    )
    run = {
        "run_id": f"q{query_count}-r{repetition}",
        "started_at": started_at,
        "finished_at": _now(),
        "configuration": {
            "query_count": query_count,
            "repetition": repetition,
            "seed_budget": seed_budget,
            "multi_query_discovery": query_count > 1,
        },
        "request": {
            "query": query,
            "filters": {},
            "evidence_mode": "scope_exhaustive",
            "score_threshold": 0,
        },
        "product_endpoint": "/api/search",
        "runtime_behavior_fingerprint": runtime_profile.get("runtime_behavior_fingerprint"),
        "expected_runtime_behavior_fingerprint": (
            expected_runtime_behavior_fingerprint
            or runtime_profile.get("runtime_behavior_fingerprint")
        ),
        "runtime_behavior_profile": runtime_profile,
        "planner": planner,
        "generated_queries": generated_queries,
        "query_hashes": query_hashes,
        "plan_fingerprint": discovery.get("plan_fingerprint") or _canonical_sha256(query_hashes),
        "discovery": {
            field: discovery.get(field)
            for field in (
                "enabled",
                "multi_query_requested",
                "multi_query_executed",
                "multi_query_query_count",
                "multi_query_status",
                "query_count",
                "generated_query_count",
                "fusion",
                "fusion_formula",
                "rrf_k",
                "final_seed_chunk_budget",
                "unique_seed_chunks",
                "unique_seed_documents",
                "duplicate_seed_ratio",
                "query_errors",
                "original_query",
                "original_query_normalized",
                "original_query_sha256",
                "generated_variants",
                "normalized_variants",
                "query_hashes",
                "plan_fingerprint",
                "timings",
            )
            if field in discovery
        },
        "requested_retrieval_profile": response.get("requested_retrieval_profile"),
        "effective_retrieval_profile": response.get("effective_retrieval_profile"),
        "retrieval_execution_complete": response.get("retrieval_execution_complete"),
        "retrieval_failure_codes": response.get("retrieval_failure_codes", []),
        "warnings": response.get("warnings", []),
        "lane_candidates": (
            response.get("effective_retrieval_profile", {}).get("lanes", {})
            if isinstance(response.get("effective_retrieval_profile"), dict)
            else {}
        ),
        "final_seeds": _compact_seeds(response),
        "scope_identities": _compact_scope(response),
        "coverage": coverage,
        "latencies": {
            "discovery_seconds": performance.get("discovery_seconds"),
            "scope_closure_seconds": performance.get("prov_o_seconds"),
            "total_latency_seconds": performance.get("total_seconds", http_wall_seconds),
            "http_wall_seconds": http_wall_seconds,
        },
        "error": response.get("error"),
    }
    run["contract"] = _contract_assessment(run)
    return run


def capture(args: argparse.Namespace) -> None:
    definition = load_ground_truth(args.definition)
    query = _canonical_query(definition)
    filters = _json_object(args.filters_json, label="filters")
    headers = _headers(args.authorization_env)
    runtime_profile_url = f"{args.base_url.rstrip('/')}/api/settings/runtime-behavior"
    runtime_profile = _get_json(runtime_profile_url, headers=headers, timeout=args.timeout)
    product_default_seed_budget = int(runtime_profile["retrieval"]["seed_budget"])
    multi_query_concurrency = int(runtime_profile["multi_query"]["concurrency"])
    seed_budget = args.seed_budget or product_default_seed_budget
    definition_sha = hashlib.sha256(args.definition.read_bytes()).hexdigest()
    repetition_plan = _repetition_plan(args)
    query_counts = sorted(repetition_plan)
    capture_value: dict[str, Any]
    if args.resume and args.output.exists():
        capture_value = json.loads(args.output.read_text(encoding="utf-8"))
    else:
        before = snapshot_product_corpus(
            args.base_url,
            headers=headers,
            page_size=args.page_size,
            timeout=args.timeout,
        )
        capture_value = {
            "schema_version": 1,
            "benchmark_id": definition["benchmark_id"],
            "benchmark_version": definition["benchmark_version"],
            "benchmark_definition_sha": definition_sha,
            "critical_contract_tag": args.contract_tag,
            "runtime_source_sha": args.runtime_source_sha,
            "started_at": _now(),
            "finished_at": None,
            "product_endpoint": f"{args.base_url.rstrip('/')}/api/search",
            "execution_path": {
                "planner_shared_with_product": True,
                "retrieval_shared_with_product": True,
                "closure_shared_with_product": True,
                "certifier_shared_with_product": True,
            },
            "context": {
                "benchmark_user_context": args.benchmark_user_context,
                "workspace": args.workspace,
                "knowledge_filters": filters,
                "dls_identity": args.dls_identity,
                "authorization_source": (
                    f"environment:{args.authorization_env}"
                    if args.authorization_env
                    else "product no-auth identity"
                ),
            },
            "runtime_behavior_profile": runtime_profile,
            "runtime_behavior_fingerprint": runtime_profile["runtime_behavior_fingerprint"],
            "planner": runtime_profile["planner"],
            "configuration": {
                "repetitions": (args.repetitions if args.repetition_plan_json is None else None),
                "repetition_plan": {
                    str(query_count): repetitions
                    for query_count, repetitions in repetition_plan.items()
                },
                "query_counts": query_counts,
                "global_seed_budget": seed_budget,
                "product_default_seed_budget": product_default_seed_budget,
                "seed_budget_source": (
                    "product_default"
                    if seed_budget == product_default_seed_budget
                    else "explicit_historical_compatibility"
                ),
                "multi_query_concurrency": multi_query_concurrency,
            },
            "corpus_before": before,
            "corpus_after": None,
            "corpus_changed": None,
            "runs": [],
        }

    completed = {str(run.get("run_id")) for run in capture_value.get("runs", [])}
    endpoint = f"{args.base_url.rstrip('/')}/api/search"
    for query_count in query_counts:
        for repetition in range(1, repetition_plan[query_count] + 1):
            run_id = f"q{query_count}-r{repetition}"
            if run_id in completed:
                continue
            body = _request_body(
                query=query,
                filters=filters,
                query_count=query_count,
                seed_budget=seed_budget,
                multi_query_concurrency=multi_query_concurrency,
            )
            run_runtime_profile = _get_json(
                runtime_profile_url, headers=headers, timeout=args.timeout
            )
            started_at = _now()
            started = time.perf_counter()
            response = _post_json(endpoint, body, headers=headers, timeout=args.timeout)
            wall = time.perf_counter() - started
            run = compact_product_response(
                response,
                query=query,
                query_count=query_count,
                repetition=repetition,
                seed_budget=seed_budget,
                started_at=started_at,
                http_wall_seconds=wall,
                runtime_profile=run_runtime_profile,
                expected_runtime_behavior_fingerprint=capture_value["runtime_behavior_fingerprint"],
            )
            run["corpus_identity_sha256"] = capture_value["corpus_before"][
                "occurrence_identity_sha256"
            ]
            run["request"]["filters"] = filters
            capture_value["runs"].append(run)
            capture_value["runs"].sort(
                key=lambda item: (
                    int(item["configuration"]["query_count"]),
                    int(item["configuration"]["repetition"]),
                )
            )
            _write_json(args.output, capture_value)

    after = snapshot_product_corpus(
        args.base_url,
        headers=headers,
        page_size=args.page_size,
        timeout=args.timeout,
    )
    for run in capture_value["runs"]:
        run["corpus_identity_sha256"] = capture_value["corpus_before"]["occurrence_identity_sha256"]
        run["contract"] = _contract_assessment(run)
    capture_value["corpus_after"] = after
    capture_value["corpus_changed"] = corpus_changed(capture_value["corpus_before"], after)
    capture_value["finished_at"] = _now()
    _write_json(args.output, capture_value)


def _identity_set(items: list[dict[str, Any]]) -> set[str]:
    identities: set[str] = set()
    for item in items:
        identity = item.get("occurrence_id") or item.get("source_entity_id")
        if identity:
            identities.add(str(identity))
        elif item.get("document_id"):
            identities.add(f"document:{item['document_id']}")
    return identities


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _range(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "mean": fmean(values) if values else None,
        "max": max(values) if values else None,
    }


def _metric_values(runs: list[dict[str, Any]], metric: str) -> list[float]:
    values: list[float] = []
    for run in runs:
        value = run["metrics"][metric]
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, int | float):
            values.append(float(value))
    return values


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [run for run in runs if run["valid"]]
    seeds = [_identity_set(run["capture"]["final_seeds"]) for run in valid]
    scopes = [_identity_set(run["capture"]["scope_identities"]) for run in valid]
    all_seeds = [_identity_set(run["capture"]["final_seeds"]) for run in runs]
    all_scopes = [_identity_set(run["capture"]["scope_identities"]) for run in runs]
    query_signatures = {
        tuple(str(item.get("query_text") or "") for item in run["capture"]["generated_queries"])
        for run in runs
    }
    result: dict[str, Any] = {
        "runs": len(runs),
        "valid_runs": len(valid),
        "invalid_run_ids": [run["run_id"] for run in runs if not run["valid"]],
        "generated_query_variants": len(query_signatures),
        "seed_jaccard": _range([_jaccard(left, right) for left, right in combinations(seeds, 2)]),
        "scope_jaccard": _range([_jaccard(left, right) for left, right in combinations(scopes, 2)]),
        "all_run_seed_jaccard": _range(
            [_jaccard(left, right) for left, right in combinations(all_seeds, 2)]
        ),
        "all_run_scope_jaccard": _range(
            [_jaccard(left, right) for left, right in combinations(all_scopes, 2)]
        ),
        "coverage_success_rate": len(
            [run for run in runs if run["capture"]["coverage"].get("complete") is True]
        )
        / len(runs)
        if runs
        else 0.0,
        "retrieval_execution_success_rate": len(
            [run for run in runs if run["capture"].get("retrieval_execution_complete") is True]
        )
        / len(runs)
        if runs
        else 0.0,
        "metrics": {},
    }
    for metric in (*_RATIO_METRICS, *_SCALAR_METRICS):
        result["metrics"][metric] = _range(_metric_values(valid, metric))
    for metric in _RATIO_METRICS:
        numerators = [float(run["metrics"][metric]["numerator"]) for run in valid]
        denominators = [int(run["metrics"][metric]["denominator"]) for run in valid]
        result["metrics"][metric]["numerator"] = _range(numerators)
        result["metrics"][metric]["denominator"] = (
            denominators[0] if denominators and len(set(denominators)) == 1 else denominators
        )
    return result


def _historical_comparison(
    summaries: dict[str, dict[str, Any]], historical: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for old in historical.get("runs", []):
        if old.get("view") != "STRICT":
            continue
        query_count = int(old["query_count"])
        if str(query_count) not in summaries["STRICT"]:
            continue
        summary = summaries["STRICT"][str(query_count)]
        for metric in (
            "seed_component_recall",
            "post_prov_o_component_recall",
            "post_prov_o_document_recall",
            "precision",
            "expansion_per_seed_document",
            "total_latency_seconds",
        ):
            if metric == "total_latency_seconds":
                old_value = float(old["performance"]["total_latency_seconds"])
            else:
                raw_old_value = old["metrics"][metric]
                if isinstance(raw_old_value, dict):
                    raw_old_value = raw_old_value.get("value")
                if not isinstance(raw_old_value, int | float):
                    raise ValueError(f"historical metric has no numeric value: {metric}")
                old_value = float(raw_old_value)
            new_value = summary["metrics"][metric]["mean"]
            rows.append(
                {
                    "query_count": query_count,
                    "metric": metric,
                    "historical": old_value,
                    "product_path_mean": new_value,
                    "delta": new_value - old_value if new_value is not None else None,
                }
            )
    return rows


def evaluate_capture(
    capture_value: dict[str, Any],
    definition: dict[str, Any],
    historical: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seed_budget = int(capture_value["configuration"]["global_seed_budget"])
    evaluated: list[dict[str, Any]] = []
    views = definition["relevance_views"]
    for run in capture_value["runs"]:
        contract = _contract_assessment(run)
        closure = {"documents": run["scope_identities"], "coverage": run["coverage"]}
        for view, labels in views.items():
            metrics = _view_metrics(
                definition,
                run["final_seeds"],
                closure,
                requested_k=seed_budget,
                labels=set(labels),
            )
            evaluated.append(
                {
                    "run_id": run["run_id"],
                    "view": view,
                    "query_count": run["configuration"]["query_count"],
                    "repetition": run["configuration"]["repetition"],
                    "valid": contract["valid"],
                    "contract_failure_codes": contract["failure_codes"],
                    "generated_queries": run["generated_queries"],
                    "requested_retrieval_profile": run["requested_retrieval_profile"],
                    "effective_retrieval_profile": run["effective_retrieval_profile"],
                    "retrieval_execution_complete": run["retrieval_execution_complete"],
                    "coverage": run["coverage"],
                    "performance": run["latencies"],
                    "metrics": {
                        **metrics,
                        "total_latency_seconds": run["latencies"]["total_latency_seconds"],
                    },
                    "capture": run,
                }
            )

    query_counts = [int(value) for value in capture_value["configuration"]["query_counts"]]
    repetition_plan = {
        int(query_count): int(repetitions)
        for query_count, repetitions in capture_value["configuration"]
        .get(
            "repetition_plan",
            {
                str(query_count): capture_value["configuration"]["repetitions"]
                for query_count in query_counts
            },
        )
        .items()
    }
    summaries: dict[str, dict[str, Any]] = {}
    for view in views:
        summaries[view] = {}
        for query_count in query_counts:
            summaries[view][str(query_count)] = _summary(
                [
                    run
                    for run in evaluated
                    if run["view"] == view and run["query_count"] == query_count
                ]
            )
    valid_strict = {
        query_count: summaries["STRICT"][str(query_count)]["valid_runs"]
        for query_count in query_counts
    }
    corpus_comparable = (
        capture_value.get("corpus_changed") is False
        and capture_value.get("corpus_before", {}).get("complete") is True
        and capture_value.get("corpus_after", {}).get("complete") is True
    )
    runtime_fingerprints = {
        str(run.get("runtime_behavior_fingerprint") or "") for run in capture_value["runs"]
    }
    runtime_behavior_stable = runtime_fingerprints == {
        str(capture_value["runtime_behavior_fingerprint"])
    }
    all_configurations_fully_valid = all(
        count == repetition_plan[query_count] for query_count, count in valid_strict.items()
    )
    best_query_count = max(
        query_counts,
        key=lambda value: (
            summaries["STRICT"][str(value)]["metrics"]["post_prov_o_component_recall"]["mean"]
            or -1,
            summaries["STRICT"][str(value)]["metrics"]["seed_component_recall"]["mean"] or -1,
            summaries["STRICT"][str(value)]["metrics"]["precision"]["mean"] or -1,
        ),
    )
    result = {
        "schema_version": 1,
        "benchmark_id": capture_value["benchmark_id"],
        "benchmark_version": capture_value["benchmark_version"],
        "critical_contract_tag": capture_value["critical_contract_tag"],
        "runtime_source_sha": capture_value["runtime_source_sha"],
        "product_endpoint": capture_value["product_endpoint"],
        "execution_path": capture_value["execution_path"],
        "context": capture_value["context"],
        "runtime_behavior_profile": capture_value["runtime_behavior_profile"],
        "runtime_behavior_fingerprint": capture_value["runtime_behavior_fingerprint"],
        "planner": capture_value["planner"],
        "configuration": capture_value["configuration"],
        "corpus": {
            "before": capture_value["corpus_before"],
            "after": capture_value["corpus_after"],
            "changed": capture_value["corpus_changed"],
        },
        "comparable": corpus_comparable,
        "runtime_behavior_stable": runtime_behavior_stable,
        "all_configurations_fully_valid": all_configurations_fully_valid,
        "runs": [
            {key: value for key, value in run.items() if key != "capture"} for run in evaluated
        ],
        "summaries": summaries,
        "best_query_count": best_query_count,
        "all_contracts_valid": all(run["valid"] for run in evaluated),
    }
    result["validation_evidence"] = [
        _validation_check(
            "product_path_only",
            observed=capture_value["product_endpoint"].endswith("/api/search"),
            expected=True,
            evidence=capture_value["execution_path"],
        ),
        _validation_check(
            "corpus_identity_stable",
            observed=capture_value.get("corpus_changed"),
            expected=False,
            evidence={
                "before": capture_value.get("corpus_before", {}).get("occurrence_identity_sha256"),
                "after": capture_value.get("corpus_after", {}).get("occurrence_identity_sha256"),
            },
        ),
        _validation_check(
            "runtime_behavior_fingerprint_stable",
            observed=runtime_behavior_stable,
            expected=True,
            evidence=sorted(runtime_fingerprints),
        ),
    ]
    result["historical_comparison"] = (
        _historical_comparison(summaries, historical) if historical else []
    )
    result["q4_gain_vs_q1"] = (
        {
            metric: summaries["STRICT"]["4"]["metrics"][metric]["mean"]
            - summaries["STRICT"]["1"]["metrics"][metric]["mean"]
            for metric in (
                "seed_component_recall",
                "post_prov_o_component_recall",
                "post_prov_o_document_recall",
            )
        }
        if {1, 4}.issubset(query_counts)
        else None
    )
    return result


def _write_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "run_id",
        "view",
        "query_count",
        "repetition",
        "valid",
        "seed_document_recall",
        "seed_component_recall",
        "post_prov_o_document_recall",
        "post_prov_o_component_recall",
        "precision",
        "expansion_per_seed_document",
        "coverage_complete",
        "retrieval_execution_complete",
        "total_latency_seconds",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for run in runs:
            metrics = run["metrics"]
            writer.writerow(
                {
                    "run_id": run["run_id"],
                    "view": run["view"],
                    "query_count": run["query_count"],
                    "repetition": run["repetition"],
                    "valid": run["valid"],
                    **{metric: metrics[metric]["value"] for metric in _RATIO_METRICS},
                    "expansion_per_seed_document": metrics["expansion_per_seed_document"],
                    "coverage_complete": run["coverage"].get("complete"),
                    "retrieval_execution_complete": run["retrieval_execution_complete"],
                    "total_latency_seconds": run["performance"]["total_latency_seconds"],
                }
            )


def _format_range(value: dict[str, Any], *, percent: bool = False) -> str:
    if value.get("mean") is None:
        return "n/a"
    if percent:
        return f"{value['mean']:.1%} [{value['min']:.1%}, {value['max']:.1%}]"
    return f"{value['mean']:.3f} [{value['min']:.3f}, {value['max']:.3f}]"


def _report(result: dict[str, Any]) -> str:
    before = result["corpus"]["before"]
    after = result["corpus"]["after"]
    lines = [
        "# Product-path discovery benchmark",
        "",
        f"benchmark_id: {result['benchmark_id']}",
        f"runtime_source_sha: {result['runtime_source_sha']}",
        f"product_endpoint: {result['product_endpoint']}",
        f"DLS_identity: {result['context']['dls_identity']}",
        f"global_seed_budget: {result['configuration']['global_seed_budget']}",
        "",
        "## Corpus",
        "",
        f"visible_occurrences: {before['visible_occurrences']}",
        f"distinct_documents: {before['distinct_document_ids']}",
        f"digest_before: {before['occurrence_identity_sha256']}",
        f"digest_after: {after['occurrence_identity_sha256']}",
        f"comparable: {str(result['comparable']).lower()}",
        "",
    ]
    query_counts = [int(value) for value in result["configuration"]["query_counts"]]
    for view in result["summaries"]:
        lines.extend(
            [
                f"## {view}",
                "",
                "| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for query_count in query_counts:
            summary = result["summaries"][view][str(query_count)]
            metrics = summary["metrics"]
            lines.append(
                f"| q{query_count} | {_format_range(metrics['seed_document_recall'], percent=True)} "
                f"| {_format_range(metrics['seed_component_recall'], percent=True)} "
                f"| {_format_range(metrics['post_prov_o_document_recall'], percent=True)} "
                f"| {_format_range(metrics['post_prov_o_component_recall'], percent=True)} "
                f"| {_format_range(metrics['precision'], percent=True)} "
                f"| {_format_range(metrics['expansion_per_seed_document'])} "
                f"| {_format_range(metrics['total_latency_seconds'])}s "
                f"| {summary['valid_runs']}/{summary['runs']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Variance",
            "",
            "| q | Query variants | Seed Jaccard | Scope Jaccard |",
            "|---:|---:|---:|---:|",
        ]
    )
    for query_count in query_counts:
        summary = result["summaries"]["STRICT"][str(query_count)]
        lines.append(
            f"| q{query_count} | {summary['generated_query_variants']} "
            f"| {_format_range(summary['seed_jaccard'])} "
            f"| {_format_range(summary['scope_jaccard'])} |"
        )
    invalid_runs = [run for run in result["runs"] if run["view"] == "STRICT" and not run["valid"]]
    lines.extend(["", "## Invalid runs", ""])
    if not invalid_runs:
        lines.append("None.")
    for run in invalid_runs:
        lines.append(
            f"- {run['run_id']}: coverage={run['coverage'].get('status_code')}; "
            f"contract={','.join(run['contract_failure_codes'])}"
        )
    if result["historical_comparison"]:
        lines.extend(
            [
                "",
                "## Historical comparison (STRICT)",
                "",
                "| q | Metric | Historical | Product mean | Delta |",
                "|---:|---|---:|---:|---:|",
            ]
        )
        for row in result["historical_comparison"]:
            percent = row["metric"] in _RATIO_METRICS
            if percent:
                values = (
                    f"{row['historical']:.1%}",
                    f"{row['product_path_mean']:.1%}",
                    f"{row['delta']:+.1%}",
                )
            else:
                values = (
                    f"{row['historical']:.3f}",
                    f"{row['product_path_mean']:.3f}",
                    f"{row['delta']:+.3f}",
                )
            lines.append(
                f"| q{row['query_count']} | {row['metric']} | {values[0]} "
                f"| {values[1]} | {values[2]} |"
            )
    lines.extend(
        [
            "",
            "## Contract audit",
            "",
            f"all_contracts_valid: {str(result['all_contracts_valid']).lower()}",
            "all_configurations_fully_valid: "
            f"{str(result['all_configurations_fully_valid']).lower()}",
            f"best_query_count_by_STRICT_post_PROV_O_component_recall: q{result['best_query_count']}",
            "q4_gain_vs_q1: " + json.dumps(result["q4_gain_vs_q1"], sort_keys=True),
            "",
            "Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.",
            "",
        ]
    )
    return "\n".join(lines)


def evaluate(args: argparse.Namespace) -> None:
    definition = load_ground_truth(args.definition)
    capture_value = json.loads(args.capture.read_text(encoding="utf-8"))
    historical = (
        json.loads(args.historical.read_text(encoding="utf-8")) if args.historical else None
    )
    result = evaluate_capture(capture_value, definition, historical)
    _write_json(args.output_json, result)
    _write_csv(args.output_csv, result["runs"])
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.write_text(_report(result), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--definition", type=Path, required=True)
    capture_parser.add_argument("--base-url", required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--repetitions", type=int, default=3)
    capture_parser.add_argument(
        "--query-counts", type=int, nargs="+", choices=(1, 2, 3, 4), default=(1, 2, 3, 4)
    )
    capture_parser.add_argument(
        "--repetition-plan-json",
        help='per-query repetition counts, for example \'{"1":5,"4":10}\'',
    )
    capture_parser.add_argument("--seed-budget", type=int)
    capture_parser.add_argument("--page-size", type=int, default=1000)
    capture_parser.add_argument("--timeout", type=int, default=900)
    capture_parser.add_argument("--filters-json", default="{}")
    capture_parser.add_argument("--authorization-env")
    capture_parser.add_argument("--benchmark-user-context", required=True)
    capture_parser.add_argument("--workspace", required=True)
    capture_parser.add_argument("--dls-identity", required=True)
    capture_parser.add_argument("--contract-tag", required=True)
    capture_parser.add_argument("--runtime-source-sha", required=True)
    capture_parser.add_argument("--resume", action="store_true")
    capture_parser.set_defaults(func=capture)

    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("--definition", type=Path, required=True)
    evaluate_parser.add_argument("--capture", type=Path, required=True)
    evaluate_parser.add_argument("--historical", type=Path)
    evaluate_parser.add_argument("--output-json", type=Path, required=True)
    evaluate_parser.add_argument("--output-csv", type=Path, required=True)
    evaluate_parser.add_argument("--output-report", type=Path, required=True)
    evaluate_parser.set_defaults(func=evaluate)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
