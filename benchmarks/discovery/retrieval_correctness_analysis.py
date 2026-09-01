"""Analyze planner amplification, natural closures, and isolated sensitivity runs."""

from __future__ import annotations

import argparse
import json
import math
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any

from benchmarks.discovery.final_baseline import _view_metrics
from benchmarks.discovery.ground_truth import load_ground_truth
from services.retrieval_service import normalize_discovery_query


def _identity_set(items: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("chunk_id") or item.get("source_entity_id") or item.get("document_id"))
        for item in items
        if item.get("chunk_id") or item.get("source_entity_id") or item.get("document_id")
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _range(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "mean": fmean(values) if values else None,
        "max": max(values) if values else None,
    }


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _plan_queries(run: dict[str, Any]) -> set[str]:
    return {
        normalize_discovery_query(str(item.get("query_text") or ""))
        for item in run.get("generated_queries", [])
        if normalize_discovery_query(str(item.get("query_text") or ""))
    }


def _plan_tokens(run: dict[str, Any]) -> set[str]:
    queries = list(run.get("generated_queries", []))[1:]
    return {
        token
        for item in queries
        for token in normalize_discovery_query(str(item.get("query_text") or "")).split()
    }


def _planner_analysis(capture: dict[str, Any]) -> dict[str, Any]:
    q1_runs = [run for run in capture["runs"] if run["configuration"]["query_count"] == 1]
    q4_runs = [run for run in capture["runs"] if run["configuration"]["query_count"] == 4]
    q1_seed_ids = _identity_set(q1_runs[0]["final_seeds"])
    pairs: list[dict[str, Any]] = []
    for left, right in combinations(q4_runs, 2):
        plan_jaccard = _jaccard(_plan_queries(left), _plan_queries(right))
        token_jaccard = _jaccard(_plan_tokens(left), _plan_tokens(right))
        seed_jaccard = _jaccard(
            _identity_set(left["final_seeds"]), _identity_set(right["final_seeds"])
        )
        scope_jaccard = _jaccard(
            _identity_set(left["scope_identities"]), _identity_set(right["scope_identities"])
        )
        left_candidates = int(
            left.get("lane_candidates", {}).get("fusion", {}).get("candidates", 0)
        )
        right_candidates = int(
            right.get("lane_candidates", {}).get("fusion", {}).get("candidates", 0)
        )
        plan_distance = 1.0 - token_jaccard
        pairs.append(
            {
                "left": left["run_id"],
                "right": right["run_id"],
                "plan_query_jaccard": plan_jaccard,
                "plan_token_jaccard": token_jaccard,
                "plan_token_distance": plan_distance,
                "candidate_count_left": left_candidates,
                "candidate_count_right": right_candidates,
                "candidate_count_absolute_difference": abs(left_candidates - right_candidates),
                "seed_jaccard": seed_jaccard,
                "seed_distance": 1.0 - seed_jaccard,
                "scope_jaccard": scope_jaccard,
                "scope_distance": 1.0 - scope_jaccard,
                "scope_to_plan_distance_ratio": (
                    (1.0 - scope_jaccard) / plan_distance if plan_distance else None
                ),
            }
        )

    q0_rows = []
    for run in q4_runs:
        seeds = run["final_seeds"]
        q0 = [item for item in seeds if "q0" in item.get("matched_queries", [])]
        q0_only = [item for item in seeds if item.get("matched_queries") == ["q0"]]
        variant_only = [item for item in seeds if "q0" not in item.get("matched_queries", [])]
        seed_ids = _identity_set(seeds)
        q0_rows.append(
            {
                "run_id": run["run_id"],
                "q0_represented": len(q0),
                "q0_only": len(q0_only),
                "variant_only": len(variant_only),
                "q1_seeds_displaced": len(q1_seed_ids - seed_ids),
            }
        )

    request_fingerprints = {
        str(run.get("planner", {}).get("request_fingerprint")) for run in q4_runs
    }
    plan_fingerprints = {str(run.get("plan_fingerprint")) for run in q4_runs}
    return {
        "q4_runs": len(q4_runs),
        "distinct_request_fingerprints": len(request_fingerprints),
        "distinct_plan_fingerprints": len(plan_fingerprints),
        "planner_models": sorted({str(run.get("planner", {}).get("model")) for run in q4_runs}),
        "response_models": sorted(
            {str(run.get("planner", {}).get("response_model")) for run in q4_runs}
        ),
        "request_parameters": q4_runs[0].get("planner", {}).get("request_parameters", {}),
        "pairwise": pairs,
        "pairwise_summary": {
            "plan_query_jaccard": _range([row["plan_query_jaccard"] for row in pairs]),
            "plan_token_jaccard": _range([row["plan_token_jaccard"] for row in pairs]),
            "candidate_count_absolute_difference": _range(
                [float(row["candidate_count_absolute_difference"]) for row in pairs]
            ),
            "seed_jaccard": _range([row["seed_jaccard"] for row in pairs]),
            "scope_jaccard": _range([row["scope_jaccard"] for row in pairs]),
            "scope_to_plan_distance_ratio": _range(
                [
                    float(row["scope_to_plan_distance_ratio"])
                    for row in pairs
                    if row["scope_to_plan_distance_ratio"] is not None
                ]
            ),
        },
        "q0_representation": q0_rows,
        "q0_representation_summary": {
            field: _range([float(row[field]) for row in q0_rows])
            for field in (
                "q0_represented",
                "q0_only",
                "variant_only",
                "q1_seeds_displaced",
            )
        },
    }


def _natural_analysis(captures: list[dict[str, Any]]) -> dict[str, Any]:
    closures = [
        item
        for capture in captures
        for item in capture.get("result", {}).get("natural_closures", [])
    ]
    values = [int(item["natural_documents"]) for item in closures]
    return {
        "closures": closures,
        "distribution": {
            "n": len(values),
            "p50": _nearest_rank(values, 0.50),
            "p90": _nearest_rank(values, 0.90),
            "p95": _nearest_rank(values, 0.95),
            "p99": _nearest_rank(values, 0.99),
            "max": max(values) if values else None,
        },
    }


def _sensitivity_analysis(
    captures: list[dict[str, Any]], definition: dict[str, Any]
) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for capture in captures:
        for experiment in capture.get("result", {}).get("sensitivity_experiments", []):
            by_id[str(experiment["experiment_id"])] = experiment
    rows = []
    for experiment in by_id.values():
        config = experiment["configuration"]
        metrics = _view_metrics(
            definition,
            experiment["seeds"],
            {"documents": experiment["documents"], "coverage": experiment["coverage"]},
            requested_k=int(config["seed_budget"]),
            labels={"CORE"},
        )
        seeds = experiment["seeds"]
        rows.append(
            {
                "experiment_id": experiment["experiment_id"],
                "axis": experiment["axis"],
                "configuration": config,
                "executed_lane_candidates": experiment.get("effective_retrieval_profile", {}).get(
                    "lanes", {}
                ),
                "seed_chunks": len(seeds),
                "seed_occurrence_diversity": len(
                    {
                        str(item.get("source_entity_id") or item.get("document_id"))
                        for item in seeds
                        if item.get("source_entity_id") or item.get("document_id")
                    }
                ),
                "scope_documents": len(experiment["documents"]),
                "coverage_complete": experiment["coverage"].get("complete") is True,
                "coverage_status": experiment["coverage"].get("status_code"),
                "total_chunks": experiment["coverage"].get("total_chunks"),
                "wall_seconds": experiment["wall_seconds"],
                "max_rss_kib_delta": experiment["max_rss_kib_delta"],
                "metrics": metrics,
            }
        )
    return {"experiments": sorted(rows, key=lambda row: row["experiment_id"])}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--product-capture", type=Path, required=True)
    parser.add_argument("--natural", type=Path, action="append", default=[])
    parser.add_argument("--sensitivity", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = {
        "schema_version": 1,
        "planner": _planner_analysis(_load(args.product_capture)),
        "natural_closure": _natural_analysis([_load(path) for path in args.natural]),
        "sensitivity": _sensitivity_analysis(
            [_load(path) for path in args.sensitivity], load_ground_truth(args.definition)
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
