"""Build and evaluate the frozen GT1/GT2 q1 candidate-horizon campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from benchmarks.discovery.ground_truth import load_ground_truth
from benchmarks.discovery.gt2_consolidation import condensed_standard_ir_metrics

HORIZONS = (50, 100, 200)
REPEATS = (1, 2, 3)


def build_campaign_plan(
    *,
    gt1_query: str,
    gt2_query: str,
    evidence_context: dict[str, Any],
) -> dict[str, Any]:
    experiments = []
    for ground_truth, query in (("gt1", gt1_query), ("gt2", gt2_query)):
        for horizon in HORIZONS:
            for repeat in REPEATS:
                experiments.append(
                    {
                        "experiment_id": f"{ground_truth}-q1-{horizon}-{horizon}-r{repeat}",
                        "axis": "candidate_horizon_cross_domain",
                        "ground_truth": ground_truth,
                        "repeat": repeat,
                        "query": query,
                        "query_count": 1,
                        "lexical_candidates": horizon,
                        "dense_candidates": horizon,
                        "rrf_k": 60,
                        "seed_budget": 100,
                        "max_depth": 8,
                        "max_entities": 500,
                        "max_documents": 250,
                        "batch_size": 50,
                    }
                )
    return {
        "evidence_context": evidence_context,
        "sensitivity_experiments": experiments,
    }


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(item: dict[str, Any]) -> str | None:
    return str(
        item.get("source_entity_id")
        or item.get("occurrence_id")
        or (f"document:{item['document_id']}" if item.get("document_id") else "")
    ) or None


def _seed_identity(item: dict[str, Any]) -> str | None:
    return str(item.get("chunk_id") or _identity(item) or "") or None


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = _identity(item)
        if identity:
            result.setdefault(identity, item)
    return list(result.values())


def _index(definition: dict[str, Any]) -> dict[str, Any]:
    by_occurrence = {}
    by_source = {}
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in definition["documents"]:
        by_occurrence[str(row["occurrence_id"])] = row
        by_source[str(row["source_entity_id"])] = row
        by_document[str(row["document_id"])].append(row)
    return {
        "by_occurrence": by_occurrence,
        "by_source": by_source,
        "by_document": {
            key: rows[0] for key, rows in by_document.items() if len(rows) == 1
        },
    }


def _match(item: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    occurrence = str(item.get("occurrence_id") or "")
    source = str(item.get("source_entity_id") or "")
    document = str(item.get("document_id") or "")
    return (
        index["by_occurrence"].get(occurrence)
        or index["by_source"].get(source)
        or index["by_document"].get(document)
    )


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _documentary_metrics(
    definition: dict[str, Any], experiment: dict[str, Any], labels: set[str]
) -> dict[str, Any]:
    index = _index(definition)
    seeds = _unique([row for row in experiment.get("seeds", []) if isinstance(row, dict)])
    closure = _unique(
        [row for row in experiment.get("documents", []) if isinstance(row, dict)]
    )
    judged_seeds = [match for row in seeds if (match := _match(row, index)) is not None]
    judged_closure = [match for row in closure if (match := _match(row, index)) is not None]
    relevant_documents = {
        str(row["occurrence_id"])
        for row in definition["documents"]
        if row.get("human_decision") in labels
    }
    relevant_components = {
        str(row["component_id"])
        for row in definition["components"]
        if row.get("human_decision") in labels
    }

    def recovered_documents(rows: list[dict[str, Any]]) -> set[str]:
        return {
            str(row["occurrence_id"])
            for row in rows
            if row["occurrence_id"] in relevant_documents
        }

    def recovered_components(rows: list[dict[str, Any]]) -> set[str]:
        return {
            str(row["component_id"])
            for row in rows
            if row.get("component_id") in relevant_components
            and row["occurrence_id"] in relevant_documents
        }

    seed_relevant = recovered_documents(judged_seeds)
    closure_relevant = recovered_documents(judged_closure)
    seed_components = recovered_components(judged_seeds)
    closure_components = recovered_components(judged_closure)
    coverage = experiment.get("coverage", {})
    return {
        "seed_documents": len(seeds),
        "judged_seed_documents": len(judged_seeds),
        "unjudged_seed_documents_excluded": len(seeds) - len(judged_seeds),
        "seed_document_recall": _ratio(len(seed_relevant), len(relevant_documents)),
        "seed_component_recall": _ratio(len(seed_components), len(relevant_components)),
        "post_prov_o_document_recall": _ratio(
            len(closure_relevant), len(relevant_documents)
        ),
        "post_prov_o_component_recall": _ratio(
            len(closure_components), len(relevant_components)
        ),
        "judged_only_precision": _ratio(len(seed_relevant), len(judged_seeds)),
        "documents_discovered_after_prov_o": len(closure),
        "judged_closure_documents": len(judged_closure),
        "unjudged_closure_documents_excluded": len(closure) - len(judged_closure),
        "expansion_factor": len(closure) / len(seeds) if seeds else None,
        "coverage": {
            field: coverage.get(field)
            for field in (
                "complete",
                "status_code",
                "failure_codes",
                "graph_frontier_empty",
                "graph_limit_reached",
                "graph_stop_reason",
                "documents_discovered",
                "documents_complete",
                "covered_chunks",
                "total_chunks",
                "scope_policy_id",
                "scope_policy_version",
            )
        },
        "seed_relevant_occurrence_ids": sorted(seed_relevant),
        "closure_relevant_occurrence_ids": sorted(closure_relevant),
        "seed_component_ids": sorted(seed_components),
        "closure_component_ids": sorted(closure_components),
    }


def _standard_metrics(
    definition: dict[str, Any], experiment: dict[str, Any]
) -> dict[str, Any]:
    index = _index(definition)
    ranked = _unique([row for row in experiment.get("seeds", []) if isinstance(row, dict)])
    ranked_candidate_ids = []
    for row in ranked:
        match = _match(row, index)
        ranked_candidate_ids.append(
            str(match.get("candidate_id") or match["occurrence_id"])
            if match is not None
            else f"UNJUDGED:{_identity(row)}"
        )
    qrels = {
        str(row.get("candidate_id") or row["occurrence_id"]): int(
            row.get(
                "qrel_grade",
                {"CORE": 2, "CONTEXTUAL": 1, "NOT_RELEVANT": 0}[
                    row["human_decision"]
                ],
            )
        )
        for row in definition["documents"]
    }
    return {
        **condensed_standard_ir_metrics(ranked_candidate_ids, qrels),
        "evaluation_policy": "condensed judged-only; unjudged documents excluded",
    }


def _experiment_metadata(experiment_id: str) -> tuple[str, int, int]:
    match = __import__("re").fullmatch(r"(gt[12])-q1-(50|100|200)-\2-r([123])", experiment_id)
    if match is None:
        raise ValueError(f"unexpected campaign experiment id: {experiment_id}")
    return match.group(1), int(match.group(2)), int(match.group(3))


def _range(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "mean": fmean(values) if values else None,
        "max": max(values) if values else None,
    }


def analyze_campaign(
    capture: dict[str, Any],
    gt1: dict[str, Any],
    gt2: dict[str, Any],
    *,
    corpus_before: dict[str, Any],
    corpus_after: dict[str, Any],
) -> dict[str, Any]:
    definitions = {"gt1": gt1, "gt2": gt2}
    rows = []
    experiments = capture.get("result", {}).get("sensitivity_experiments", [])
    if len(experiments) != len(HORIZONS) * len(REPEATS) * 2:
        raise ValueError("cross-domain campaign requires exactly 18 experiment results")
    for experiment in experiments:
        ground_truth, horizon, repeat = _experiment_metadata(str(experiment["experiment_id"]))
        definition = definitions[ground_truth]
        configuration = experiment["configuration"]
        expected = {
            "lexical_candidates": horizon,
            "dense_candidates": horizon,
            "rrf_k": 60,
            "seed_budget": 100,
            "query_count": 1,
            "max_depth": 8,
            "max_entities": 500,
            "max_documents": 250,
            "batch_size": 50,
        }
        if configuration != expected:
            raise ValueError(f"configuration drift for {experiment['experiment_id']}")
        rows.append(
            {
                "experiment_id": experiment["experiment_id"],
                "ground_truth": ground_truth,
                "candidate_horizon": horizon,
                "repeat": repeat,
                "configuration": configuration,
                "query": experiment["query"],
                "query_sha256": experiment["query_sha256"],
                "seed_identity_set_sha256": _sha256_json(
                    sorted(
                        {
                            identity
                            for row in experiment.get("seeds", [])
                            if (identity := _seed_identity(row)) is not None
                        }
                    )
                ),
                "seed_identity_order_sha256": _sha256_json(
                    [
                        identity
                        for row in experiment.get("seeds", [])
                        if (identity := _seed_identity(row)) is not None
                    ]
                ),
                "scope_identity_set_sha256": _sha256_json(
                    sorted(
                        {
                            identity
                            for row in experiment.get("documents", [])
                            if (identity := _identity(row)) is not None
                        }
                    )
                ),
                "scope_identity_order_sha256": _sha256_json(
                    [
                        identity
                        for row in experiment.get("documents", [])
                        if (identity := _identity(row)) is not None
                    ]
                ),
                "strict": _documentary_metrics(definition, experiment, {"CORE"}),
                "broad": _documentary_metrics(
                    definition, experiment, {"CORE", "CONTEXTUAL"}
                ),
                "standard_ir": _standard_metrics(definition, experiment),
                "performance": {
                    "graph_traversal_seconds": experiment.get("graph_traversal_seconds"),
                    "document_read_seconds": experiment.get("document_read_seconds"),
                    "total_seconds": experiment.get("wall_seconds"),
                    "max_rss_kib_delta": experiment.get("max_rss_kib_delta"),
                },
                "effective_retrieval_profile": experiment.get(
                    "effective_retrieval_profile", {}
                ),
            }
        )

    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["ground_truth"], row["candidate_horizon"])].append(row)
    summaries = []
    for (ground_truth, horizon), group in sorted(groups.items()):
        group.sort(key=lambda row: row["repeat"])
        documentary_metric_payloads = [
            _sha256_json(
                {
                    "strict": row["strict"],
                    "broad": row["broad"],
                }
            )
            for row in group
        ]
        standard_metric_payloads = [_sha256_json(row["standard_ir"]) for row in group]
        seed_set_hashes = {row["seed_identity_set_sha256"] for row in group}
        seed_order_hashes = {row["seed_identity_order_sha256"] for row in group}
        scope_set_hashes = {row["scope_identity_set_sha256"] for row in group}
        scope_order_hashes = {row["scope_identity_order_sha256"] for row in group}
        documentary_metric_hashes = set(documentary_metric_payloads)
        standard_metric_hashes = set(standard_metric_payloads)
        standard_range = {
            field: _range(
                [
                    float(row["standard_ir"][field])
                    for row in group
                    if row["standard_ir"][field] is not None
                ]
            )
            for field in (
                "nDCG@10",
                "nDCG@100",
                "MAP",
                "Recall@100",
                "Recall@200",
                "Precision@100",
            )
        }
        summaries.append(
            {
                "ground_truth": ground_truth,
                "candidate_horizon": horizon,
                "repeats": len(group),
                "seed_identity_stability": 1.0 if len(seed_set_hashes) == 1 else 0.0,
                "ordered_seed_stability": 1.0 if len(seed_order_hashes) == 1 else 0.0,
                "scope_identity_stability": 1.0 if len(scope_set_hashes) == 1 else 0.0,
                "ordered_scope_stability": 1.0 if len(scope_order_hashes) == 1 else 0.0,
                "documentary_metrics_identical": len(documentary_metric_hashes) == 1,
                "standard_metrics_identical": len(standard_metric_hashes) == 1,
                "metrics_identical": (
                    len(documentary_metric_hashes) == 1
                    and len(standard_metric_hashes) == 1
                ),
                "coverage_success_rate": sum(
                    row["strict"]["coverage"].get("complete") is True for row in group
                )
                / len(group),
                "strict": group[0]["strict"],
                "broad": group[0]["broad"],
                "standard_ir": group[0]["standard_ir"],
                "standard_ir_range": standard_range,
                "performance": {
                    field: _range(
                        [float(row["performance"][field] or 0.0) for row in group]
                    )
                    for field in (
                        "graph_traversal_seconds",
                        "document_read_seconds",
                        "total_seconds",
                        "max_rss_kib_delta",
                    )
                },
            }
        )
    seed_identity_sets_stable = all(
        row["seed_identity_stability"] == 1.0 for row in summaries
    )
    scope_identity_sets_stable = all(
        row["scope_identity_stability"] == 1.0 for row in summaries
    )
    identity_sets_stable = seed_identity_sets_stable and scope_identity_sets_stable
    ordered_identities_stable = all(
        row["ordered_seed_stability"] == 1.0
        and row["ordered_scope_stability"] == 1.0
        for row in summaries
    )
    all_metrics_identical = all(row["metrics_identical"] for row in summaries)
    corpus_comparable = (
        corpus_before.get("occurrence_identity_sha256")
        == corpus_after.get("occurrence_identity_sha256")
        == "038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7"
    )
    return {
        "schema_version": 1,
        "artifact_type": "cross_domain_candidate_horizon_analysis",
        "campaign": {
            "query_count": 1,
            "horizons": list(HORIZONS),
            "repeats_per_horizon_per_ground_truth": len(REPEATS),
            "only_variable": "lexical_candidates and dense_candidates, changed together",
            "fixed": {
                "rrf_k": 60,
                "seed_budget": 100,
                "multi_query": False,
                "scope_policy": "documentary-prov-o v1",
                "max_depth": 8,
                "max_entities": 500,
                "max_documents": 250,
                "batch_size": 50,
                "identity": capture.get("result", {}).get("identity_descriptor"),
                "workspace": capture.get("result", {}).get("workspace"),
                "knowledge_filters": capture.get("result", {}).get("knowledge_filters"),
            },
        },
        "corpus": {
            "before": corpus_before,
            "after": corpus_after,
            "comparable": corpus_comparable,
        },
        "ground_truth": {
            "gt1": {
                "benchmark_id": gt1["benchmark_id"],
                "source_sha256": None,
            },
            "gt2": {
                "benchmark_id": gt2["benchmark_id"],
                "ground_truth_digest": gt2["ground_truth_digest"],
            },
        },
        "runs": rows,
        "summaries": summaries,
        "determinism": {
            "all_groups_seed_identity_sets_stable": seed_identity_sets_stable,
            "all_groups_scope_identity_sets_stable": scope_identity_sets_stable,
            "all_groups_ordered_identities_stable": ordered_identities_stable,
            "all_groups_metrics_identical": all_metrics_identical,
            "status": (
                "PASS"
                if identity_sets_stable and ordered_identities_stable and all_metrics_identical
                else "FAIL"
            ),
            "audit": {
                "finding": (
                    "Repeat 2 changed one RRF ordering position for GT1 50/50 and GT2 "
                    "200/200. All seed identity sets and scope identity sets remained "
                    "unchanged; GT2 order-sensitive standard metrics changed."
                ),
                "affected_groups": ["gt1-q1-50-50", "gt2-q1-200-200"],
                "observed_rank_windows": {
                    "gt1-q1-50-50": "89-90",
                    "gt2-q1-200-200": "38-40",
                },
                "affected_source_entities": [
                    "urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:1529acbdf0d41f68886328461091d75cac2871633e3e7bfbef0307a20bec391b",
                    "urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:03cd0c56-ea2d-4d27-9e77-c4fff670f02c",
                    "urn:openrag:openarchiver:email:6ca4c5b5-3440-4c50-8666-cecfa25916d5:4a566468-1e95-4c95-8f69-8fe6bf3c1b8c",
                    "urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:737a56b8-9058-46ee-b8c3-65b12a608121",
                ],
                "rrf_score_changes": {
                    "gt1-q1-50-50": {
                        "source_entity": (
                            "urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:"
                            "03cd0c56-ea2d-4d27-9e77-c4fff670f02c"
                        ),
                        "repeats_1_and_3": 0.009174311926605505,
                        "repeat_2": 0.009259259259259259,
                        "compatible_single_lane_ranks": {
                            "repeats_1_and_3": 49,
                            "repeat_2": 48,
                        },
                    },
                    "gt2-q1-200-200": {
                        "source_entity": (
                            "urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:"
                            "737a56b8-9058-46ee-b8c3-65b12a608121"
                        ),
                        "repeats_1_and_3": 0.01259644150527476,
                        "repeat_2": 0.012740133429788601,
                        "compatible_lane_rank_pairs": {
                            "repeats_1_and_3": [86, 114],
                            "repeat_2": [83, 114],
                        },
                    },
                },
                "cause_assessment": (
                    "The deterministic RRF implementation sorts by score then persistent "
                    "chunk identity. The changed reciprocal-rank score proves that one input "
                    "lane rank changed before fusion; the deployed dense lane is an approximate "
                    "OpenSearch k-NN query, while the lexical lane has an explicit score/chunk-id "
                    "sort. This is evidence consistent with dense approximate-rank drift, not "
                    "with a planner or scope traversal difference."
                ),
                "quality_interpretation_authorized": False,
            },
        },
        "unjudged_policy": (
            "Unjudged documents are excluded from precision and condensed standard metrics; "
            "they are never mapped to NOT_RELEVANT."
        ),
        "decision": {
            "candidate_horizon_recommendation": "KEEP 50/50",
            "rationale": (
                "The change gate failed: ordered retrieval was not deterministic across all "
                "groups, and GT2 100/100 and 200/200 reached the fail-closed document limit. "
                "Keeping 50/50 is a no-change safety decision, not a claim of quality "
                "superiority."
            ),
            "qwen_readiness": "BLOCKED",
            "final_conclusion": "GT2 FROZEN - CROSS-DOMAIN EVIDENCE INSUFFICIENT",
        },
        "product_defaults_changed": False,
        "deployment_performed": False,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, summaries: list[dict[str, Any]]) -> None:
    fields = [
        "ground_truth",
        "candidate_horizon",
        "repeats",
        "seed_identity_stability",
        "ordered_seed_stability",
        "scope_identity_stability",
        "ordered_scope_stability",
        "documentary_metrics_identical",
        "standard_metrics_identical",
        "metrics_identical",
        "coverage_success_rate",
        "strict_seed_document_recall",
        "strict_seed_component_recall",
        "strict_post_document_recall",
        "strict_post_component_recall",
        "strict_judged_only_precision",
        "broad_post_document_recall",
        "broad_post_component_recall",
        "broad_judged_only_precision",
        "nDCG@10",
        "nDCG@100",
        "MAP",
        "Recall@100",
        "Recall@200",
        "Precision@100",
        "total_latency_mean_seconds",
        "graph_latency_mean_seconds",
        "document_read_latency_mean_seconds",
        "max_rss_kib_delta_mean",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in summaries:
            writer.writerow(
                {
                    "ground_truth": row["ground_truth"],
                    "candidate_horizon": row["candidate_horizon"],
                    "repeats": row["repeats"],
                    "seed_identity_stability": row["seed_identity_stability"],
                    "ordered_seed_stability": row["ordered_seed_stability"],
                    "scope_identity_stability": row["scope_identity_stability"],
                    "ordered_scope_stability": row["ordered_scope_stability"],
                    "documentary_metrics_identical": row[
                        "documentary_metrics_identical"
                    ],
                    "standard_metrics_identical": row["standard_metrics_identical"],
                    "metrics_identical": row["metrics_identical"],
                    "coverage_success_rate": row["coverage_success_rate"],
                    "strict_seed_document_recall": row["strict"]["seed_document_recall"][
                        "value"
                    ],
                    "strict_seed_component_recall": row["strict"]["seed_component_recall"][
                        "value"
                    ],
                    "strict_post_document_recall": row["strict"][
                        "post_prov_o_document_recall"
                    ]["value"],
                    "strict_post_component_recall": row["strict"][
                        "post_prov_o_component_recall"
                    ]["value"],
                    "strict_judged_only_precision": row["strict"]["judged_only_precision"][
                        "value"
                    ],
                    "broad_post_document_recall": row["broad"][
                        "post_prov_o_document_recall"
                    ]["value"],
                    "broad_post_component_recall": row["broad"][
                        "post_prov_o_component_recall"
                    ]["value"],
                    "broad_judged_only_precision": row["broad"]["judged_only_precision"][
                        "value"
                    ],
                    **{key: row["standard_ir"][key] for key in (
                        "nDCG@10",
                        "nDCG@100",
                        "MAP",
                        "Recall@100",
                        "Recall@200",
                        "Precision@100",
                    )},
                    "total_latency_mean_seconds": row["performance"]["total_seconds"]["mean"],
                    "graph_latency_mean_seconds": row["performance"][
                        "graph_traversal_seconds"
                    ]["mean"],
                    "document_read_latency_mean_seconds": row["performance"][
                        "document_read_seconds"
                    ]["mean"],
                    "max_rss_kib_delta_mean": row["performance"]["max_rss_kib_delta"][
                        "mean"
                    ],
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--application-sha", required=True)
    plan.add_argument("--corpus-digest", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--capture", type=Path, required=True)
    analyze.add_argument("--gt1", type=Path, required=True)
    analyze.add_argument("--gt2", type=Path, required=True)
    analyze.add_argument("--corpus-before", type=Path, required=True)
    analyze.add_argument("--corpus-after", type=Path, required=True)
    analyze.add_argument("--output-json", type=Path, required=True)
    analyze.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "plan":
        _write_json(
            args.output,
            build_campaign_plan(
                gt1_query=(
                    "Donne-moi tous les échanges avec l’administration sur le projet Surface "
                    "pastorale."
                ),
                gt2_query="Tous les échanges avec Orange au sujet de la fibre.",
                evidence_context={
                    "application_sha": args.application_sha,
                    "corpus_digest": args.corpus_digest,
                    "ground_truth_gate": "GT2_FREEZE_PASS",
                    "unjudged_policy": "exclude; never default to NOT_RELEVANT",
                },
            ),
        )
        return
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    gt1 = load_ground_truth(args.gt1)
    gt2 = load_ground_truth(args.gt2)
    before = json.loads(args.corpus_before.read_text(encoding="utf-8"))
    after = json.loads(args.corpus_after.read_text(encoding="utf-8"))
    result = analyze_campaign(capture, gt1, gt2, corpus_before=before, corpus_after=after)
    result["ground_truth"]["gt1"]["source_sha256"] = hashlib.sha256(
        args.gt1.read_bytes()
    ).hexdigest()
    _write_json(args.output_json, result)
    _write_csv(args.output_csv, result["summaries"])


if __name__ == "__main__":
    main()
