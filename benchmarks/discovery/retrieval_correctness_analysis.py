"""Analyze planner amplification, natural closures, and isolated sensitivity runs."""

from __future__ import annotations

import argparse
import json
import math
import re
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


def _query_tokens(value: str) -> set[str]:
    return set(normalize_discovery_query(value).split())


def _query_lengths(run: dict[str, Any]) -> list[int]:
    return [
        len(normalize_discovery_query(str(item.get("query_text") or "")).split())
        for item in run.get("generated_queries", [])[1:]
    ]


def _within_plan_token_jaccards(run: dict[str, Any]) -> list[float]:
    variants = [
        _query_tokens(str(item.get("query_text") or ""))
        for item in run.get("generated_queries", [])[1:]
    ]
    return [_jaccard(left, right) for left, right in combinations(variants, 2)]


def _literal_anchor_tokens(query: str) -> set[str]:
    """Return syntax-derived anchors without pretending to perform NER."""

    quoted = re.findall(r'["«](.*?)["»]', query)
    raw_tokens = re.findall(r"[^\W_]+(?:[-_][^\W_]+)*", query, flags=re.UNICODE)
    anchors = {
        normalize_discovery_query(token)
        for index, token in enumerate(raw_tokens)
        if any(char.isdigit() for char in token)
        or (len(token) > 1 and token.isupper())
        or (index > 0 and token[:1].isupper())
    }
    for phrase in quoted:
        anchors.update(_query_tokens(phrase))
    return {token for token in anchors if token}


def _semantic_drift_diagnostics(run: dict[str, Any]) -> dict[str, Any]:
    queries = list(run.get("generated_queries", []))
    original = str(queries[0].get("query_text") or "") if queries else ""
    original_tokens = _query_tokens(original)
    anchors = _literal_anchor_tokens(original)
    variants = []
    for item in queries[1:]:
        text = str(item.get("query_text") or "")
        tokens = _query_tokens(text)
        variants.append(
            {
                "query_id": item.get("query_id"),
                "token_count": len(tokens),
                "original_token_retention": (
                    len(tokens & original_tokens) / len(original_tokens) if original_tokens else 1.0
                ),
                "literal_anchors_preserved": sorted(tokens & anchors),
                "literal_anchors_dropped": sorted(anchors - tokens),
                "introduced_tokens": sorted(tokens - original_tokens),
            }
        )
    raw_normalized = [
        str(item.get("normalized_text") or "")
        for item in run.get("normalized_variants", [])
        if item.get("normalized_text")
    ]
    within_plan = _within_plan_token_jaccards(run)
    return {
        "run_id": run["_analysis_id"],
        "automatic_semantic_verdict": None,
        "interpretation": (
            "diagnostic proxies only; breadth, narrowness, and unrelatedness require review"
        ),
        "literal_anchor_method": "quoted, identifier-like, acronym, or mid-query titlecase tokens",
        "literal_anchors": sorted(anchors),
        "raw_variant_count": len(raw_normalized),
        "duplicate_normalized_variants": len(raw_normalized) - len(set(raw_normalized)),
        "within_plan_variant_token_jaccard": _range(within_plan),
        "variants": variants,
    }


def _occurrence_id(item: dict[str, Any]) -> str | None:
    value = item.get("occurrence_id") or item.get("source_entity_id")
    return str(value) if value not in (None, "") else None


def _component_set(
    items: list[dict[str, Any]], component_by_occurrence: dict[str, str]
) -> set[str]:
    return {
        component_by_occurrence[occurrence]
        for item in items
        for occurrence in [_occurrence_id(item)]
        if occurrence in component_by_occurrence
    }


def _query_contribution_summary(seeds: list[dict[str, Any]]) -> dict[str, Any]:
    query_ids = sorted(
        {
            str(query_id)
            for seed in seeds
            for query_id in seed.get("matched_queries", [])
            if query_id not in (None, "")
        }
    )
    rows: dict[str, Any] = {}
    for query_id in query_ids:
        matched = [seed for seed in seeds if query_id in seed.get("matched_queries", [])]
        contributions = [
            contribution
            for seed in matched
            for contribution in seed.get("query_contributions", [])
            if isinstance(contribution, dict) and contribution.get("query_id") == query_id
        ]
        rows[query_id] = {
            "matched_seed_count": len(matched),
            "exclusive_seed_count": sum(
                seed.get("matched_queries", []) == [query_id] for seed in matched
            ),
            "global_rrf_contribution_sum": sum(
                float(value.get("global_rrf_contribution") or 0.0) for value in contributions
            ),
            "lane_memberships": {
                lane: sum(lane in value.get("matched_lanes", []) for value in contributions)
                for lane in ("lexical", "dense")
            },
        }
    return rows


def _planner_analysis(captures: list[dict[str, Any]], definition: dict[str, Any]) -> dict[str, Any]:
    runs = [
        {**run, "_analysis_id": f"capture-{capture_index}:{run['run_id']}"}
        for capture_index, capture in enumerate(captures, start=1)
        for run in capture["runs"]
    ]
    q1_runs = [run for run in runs if run["configuration"]["query_count"] == 1]
    q4_runs = [run for run in runs if run["configuration"]["query_count"] == 4]
    if not q1_runs:
        raise ValueError("planner analysis requires at least one q1 product run")
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
                "left": left["_analysis_id"],
                "right": right["_analysis_id"],
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

    relevant_components = {
        str(item["component_id"])
        for item in definition.get("components", [])
        if item.get("human_decision") == "CORE"
    }
    component_by_occurrence = {
        str(item["occurrence_id"]): str(item["component_id"])
        for item in definition.get("documents", [])
        if item.get("human_decision") == "CORE" and item.get("component_id") in relevant_components
    }
    q1_seed_occurrences = {
        occurrence
        for item in q1_runs[0]["final_seeds"]
        for occurrence in [_occurrence_id(item)]
        if occurrence is not None
    }
    q1_relevant_occurrences = q1_seed_occurrences & set(component_by_occurrence)
    q1_seed_components = _component_set(q1_runs[0]["final_seeds"], component_by_occurrence)
    q1_scope_components = _component_set(q1_runs[0]["scope_identities"], component_by_occurrence)

    q0_rows = []
    for run in q4_runs:
        seeds = run["final_seeds"]
        q0 = [item for item in seeds if "q0" in item.get("matched_queries", [])]
        q0_only = [item for item in seeds if item.get("matched_queries") == ["q0"]]
        variant_only = [item for item in seeds if "q0" not in item.get("matched_queries", [])]
        seed_ids = _identity_set(seeds)
        seed_occurrences = {
            occurrence
            for item in seeds
            for occurrence in [_occurrence_id(item)]
            if occurrence is not None
        }
        variant_only_occurrences = {
            occurrence
            for item in variant_only
            for occurrence in [_occurrence_id(item)]
            if occurrence is not None
        }
        seed_components = _component_set(seeds, component_by_occurrence)
        scope_components = _component_set(run["scope_identities"], component_by_occurrence)
        q0_rows.append(
            {
                "run_id": run["_analysis_id"],
                "q0_represented": len(q0),
                "q0_only": len(q0_only),
                "variant_only": len(variant_only),
                "q1_seeds_displaced": len(q1_seed_ids - seed_ids),
                "relevant_q1_seed_occurrences_evicted": len(
                    q1_relevant_occurrences - seed_occurrences
                ),
                "unique_relevant_variant_occurrences_added": len(
                    (variant_only_occurrences & set(component_by_occurrence))
                    - q1_relevant_occurrences
                ),
                "net_seed_component_gain": len(seed_components - q1_seed_components)
                - len(q1_seed_components - seed_components),
                "net_scope_component_gain": len(scope_components - q1_scope_components)
                - len(q1_scope_components - scope_components),
                "query_contributions": _query_contribution_summary(seeds),
            }
        )

    request_fingerprints = {
        str(run.get("planner", {}).get("request_fingerprint")) for run in q4_runs
    }
    plan_fingerprints = {str(run.get("plan_fingerprint")) for run in q4_runs}
    diagnostics = [_semantic_drift_diagnostics(run) for run in q4_runs]
    query_lengths = [length for run in q4_runs for length in _query_lengths(run)]
    within_plan_jaccards = [value for run in q4_runs for value in _within_plan_token_jaccards(run)]
    return {
        "q4_runs": len(q4_runs),
        "distinct_request_fingerprints": len(request_fingerprints),
        "distinct_plan_fingerprints": len(plan_fingerprints),
        "planner_models": sorted({str(run.get("planner", {}).get("model")) for run in q4_runs}),
        "response_models": sorted(
            {str(run.get("planner", {}).get("response_model")) for run in q4_runs}
        ),
        "request_parameters": q4_runs[0].get("planner", {}).get("request_parameters", {}),
        "query_diagnostics": {
            "query_length_tokens": _range([float(value) for value in query_lengths]),
            "within_plan_variant_token_jaccard": _range(within_plan_jaccards),
            "duplicate_normalized_queries": sum(
                int(row["duplicate_normalized_variants"]) for row in diagnostics
            ),
            "semantic_drift": diagnostics,
        },
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
                "relevant_q1_seed_occurrences_evicted",
                "unique_relevant_variant_occurrences_added",
                "net_seed_component_gain",
                "net_scope_component_gain",
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


def _target_validation_analysis(
    captures: list[dict[str, Any]], resource_captures: list[dict[str, Any]]
) -> dict[str, Any]:
    cases = [
        item
        for capture in captures
        for item in capture.get("result", {}).get("documentary_target_validation", [])
        if isinstance(item, dict)
    ]
    scope_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    fixed_by_identity: dict[tuple[str, str, int], dict[str, Any]] = {}
    configuration_rows: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    for item in cases:
        validation = item["documentary_target_validation"]
        identity = (str(item["query_sha256"]), str(item.get("fixed_plan_sha256") or ""))
        if validation["state"] == "NATURAL_COMPLETE":
            scope_by_identity.setdefault(
                identity,
                {
                    "label": item["label"],
                    "documents": int(validation["final_observation"]["documents"]),
                    "entities": int(validation["final_observation"]["entities"]),
                    "chunks": int(validation["final_observation"]["chunks"]),
                    "depth": int(validation["final_observation"]["depth"]),
                },
            )
        for fixed in item["fixed_strategies"]:
            fixed_by_identity.setdefault((*identity, int(fixed["document_limit"])), fixed)
        configuration_rows.append(
            {
                "label": item["label"],
                "identity": ":".join(identity),
                "target_threshold": int(validation["target_threshold"]),
                "validation_probe_size": int(validation["validation_probe_size"]),
                "hard_safety_limit": int(validation["hard_safety_limit"]),
                "state": validation["state"],
                "coverage_complete": validation["coverage_complete"],
                "false_target_at_threshold": validation["false_target_at_threshold"],
                "number_of_probes": int(validation["number_of_probes"]),
                "target_extensions": int(validation["target_extensions"]),
                "final_target": int(validation["final_target"]),
                "final_documents": int(validation["final_observation"]["documents"]),
                "prototype_replay_graph_latency_seconds": float(
                    validation["prototype_replay_graph_latency_seconds"]
                ),
            }
        )
        probes.extend({"label": item["label"], **probe} for probe in validation.get("probes", []))

    closure_values = [int(value["documents"]) for value in scope_by_identity.values()]
    fixed_groups: dict[int, list[dict[str, Any]]] = {}
    for (*_identity, limit), row in fixed_by_identity.items():
        fixed_groups.setdefault(limit, []).append(row)
    fixed_summary = {
        str(limit): {
            "closures": len(rows),
            "coverage_success_rate": sum(row["coverage_success"] is True for row in rows)
            / len(rows),
            "legitimate_truncation_rate": sum(
                row["truncated_legitimate_closure"] is True for row in rows
            )
            / len(rows),
            "documents": _range([float(row["documents"]) for row in rows]),
            "chunks": _range([float(row["chunks"]) for row in rows]),
            "graph_latency_seconds": _range([float(row["graph_latency_seconds"]) for row in rows]),
        }
        for limit, rows in sorted(fixed_groups.items())
    }

    validation_groups: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for row in configuration_rows:
        key = (
            row["target_threshold"],
            row["validation_probe_size"],
            row["hard_safety_limit"],
        )
        validation_groups.setdefault(key, []).append(row)
    validation_summary = {
        f"target-{key[0]}-probe-{key[1]}-hard-{key[2]}": {
            "target_threshold": key[0],
            "validation_probe_size": key[1],
            "hard_safety_limit": key[2],
            "queries": len(rows),
            "closures_completed_before_target": sum(
                row["coverage_complete"] and not row["false_target_at_threshold"] for row in rows
            ),
            "closures_requiring_probe": sum(row["number_of_probes"] > 0 for row in rows),
            "closures_requiring_multiple_extensions": sum(
                row["target_extensions"] > 1 for row in rows
            ),
            "hard_limit_hits": sum(row["state"] == "HARD_SAFETY_LIMIT_REACHED" for row in rows),
            "false_target_rate": sum(row["false_target_at_threshold"] for row in rows) / len(rows),
            "coverage_success_rate": sum(row["coverage_complete"] for row in rows) / len(rows),
            "probe_count": _range([float(row["number_of_probes"]) for row in rows]),
            "prototype_replay_graph_latency_seconds": _range(
                [float(row["prototype_replay_graph_latency_seconds"]) for row in rows]
            ),
        }
        for key, rows in sorted(validation_groups.items())
    }

    yields: dict[int, list[dict[str, Any]]] = {}
    for probe in probes:
        yields.setdefault(int(probe["new_documents"]), []).append(probe)
    frontier_dynamics_examples = [
        {
            "new_documents": value,
            "frontier_growth_rates": sorted(
                {
                    float(row["frontier_growth_rate"])
                    for row in rows
                    if row.get("frontier_growth_rate") is not None
                }
            ),
            "depth_transitions": sorted(
                {(int(row["depth_before"]), int(row["depth_after"])) for row in rows}
            ),
        }
        for value, rows in sorted(yields.items())
        if len(
            {
                round(float(row["frontier_growth_rate"]), 9)
                for row in rows
                if row.get("frontier_growth_rate") is not None
            }
        )
        > 1
    ]

    resource_rows = [
        experiment
        for capture in resource_captures
        for experiment in capture.get("result", {}).get("sensitivity_experiments", [])
        if experiment.get("axis") == "scope_limit_strategy"
    ]
    resources = []
    for row in resource_rows:
        identity = (str(row["query_sha256"]), str(row.get("fixed_plan_sha256") or ""))
        natural = scope_by_identity.get(identity)
        documents = len(row.get("documents", []))
        resources.append(
            {
                "experiment_id": row["experiment_id"],
                "document_limit": int(row["configuration"]["max_documents"]),
                "documents": documents,
                "entities": int(row.get("coverage", {}).get("graph_entities_visited") or 0),
                "chunks": int(row.get("coverage", {}).get("total_chunks") or 0),
                "coverage_complete": row.get("coverage", {}).get("complete") is True,
                "stop_reason": row.get("coverage", {}).get("graph_stop_reason"),
                "natural_closure_recovery": (
                    documents / int(natural["documents"]) if natural else None
                ),
                "graph_traversal_seconds": float(row.get("graph_traversal_seconds") or 0.0),
                "document_read_seconds": float(row.get("document_read_seconds") or 0.0),
                "total_seconds": float(row.get("wall_seconds") or 0.0),
                "max_rss_kib_delta": int(row.get("max_rss_kib_delta") or 0),
            }
        )

    return {
        "documentary_semantics": "probe-beyond-target-to-validate-target",
        "target_thresholds_tested": sorted(
            {int(row["target_threshold"]) for row in configuration_rows}
        ),
        "validation_probe_sizes_tested": sorted(
            {int(row["validation_probe_size"]) for row in configuration_rows}
        ),
        "hard_safety_limits_tested": sorted(
            {int(row["hard_safety_limit"]) for row in configuration_rows}
        ),
        "natural_closure": {
            "queries": len(scope_by_identity),
            "closures": list(scope_by_identity.values()),
            "distribution": {
                "p50": _nearest_rank(closure_values, 0.50),
                "p90": _nearest_rank(closure_values, 0.90),
                "p95": _nearest_rank(closure_values, 0.95),
                "p99": _nearest_rank(closure_values, 0.99),
                "max": max(closure_values) if closure_values else None,
            },
        },
        "fixed_strategies": fixed_summary,
        "target_validation_strategies": validation_summary,
        "probe_structural_signals": {
            "signals": [
                "frontier_before/after",
                "new_documents/entities/documentary_relations/connected_branches",
                "depth_before/after",
                "relation_type_delta",
                "already_covered_ratio",
                "hub_degree_before/after",
                "frontier_growth_rate",
            ],
            "marginal_yield_alone_sufficient": not bool(frontier_dynamics_examples),
            "frontier_dynamics_examples": frontier_dynamics_examples,
        },
        "resource_experiments": resources,
        "configuration_rows": configuration_rows,
        "probes": probes,
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--product-capture", type=Path, action="append", required=True)
    parser.add_argument("--natural", type=Path, action="append", default=[])
    parser.add_argument("--sensitivity", type=Path, action="append", default=[])
    parser.add_argument("--target-validation", type=Path, action="append", default=[])
    parser.add_argument("--scope-resource", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    definition = load_ground_truth(args.definition)
    product_captures = [_load(path) for path in args.product_capture]
    result = {
        "schema_version": 1,
        "planner": _planner_analysis(product_captures, definition),
        "natural_closure": _natural_analysis([_load(path) for path in args.natural]),
        "sensitivity": _sensitivity_analysis(
            [_load(path) for path in args.sensitivity], definition
        ),
        "documentary_target_validation": _target_validation_analysis(
            [_load(path) for path in args.target_validation],
            [_load(path) for path in args.scope_resource],
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
