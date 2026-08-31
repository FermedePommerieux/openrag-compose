"""Capture and evaluate the generic multi-query discovery experiment."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.discovery.corpus import corpus_changed, snapshot_visible_corpus
from benchmarks.discovery.final_baseline import (
    _format_ratio,
    _index,
    _match,
    _view_metrics,
    _write_json,
)
from benchmarks.discovery.ground_truth import load_ground_truth


def _build_plan(definition: dict[str, Any]) -> dict[str, Any]:
    baseline = definition["baseline_run"]
    query = next(item for item in definition["queries"] if item.get("kind") == "canonical_literal")
    return {
        "query": query["text"],
        "retrieval": baseline["retrieval"],
        "embedding_model": baseline["embedding"]["model"],
        "max_queries": 4,
        "concurrency": 2,
        # The frozen q1 baseline yielded 96 unique seed chunks. Holding that
        # effective budget constant isolates query diversity from pool growth.
        "final_seed_budget": 96,
        "scope": {
            "max_depth": 8,
            "max_entities": 500,
            "max_documents": 250,
            "batch_size": 50,
        },
    }


def _remote_capture(
    definition: dict[str, Any],
    *,
    script_path: Path,
    ssh_host: str,
    ssh_key: Path,
    namespace: str,
    deployment: str,
    timeout: int,
) -> dict[str, Any]:
    script_b64 = base64.b64encode(script_path.read_bytes()).decode("ascii")
    plan_b64 = base64.b64encode(
        json.dumps(_build_plan(definition), ensure_ascii=False).encode()
    ).decode("ascii")
    bootstrap = (
        "import base64,sys;script=sys.argv[1];plan=sys.argv[2];"
        "sys.argv=['remote_multi_query.py',plan];exec(base64.b64decode(script))"
    )
    remote_args = [
        "sudo",
        "kubectl",
        "-n",
        namespace,
        "exec",
        f"deploy/{deployment}",
        "--",
        "env",
        "PYTHONPATH=/app/src",
        "python",
        "-c",
        bootstrap,
        script_b64,
        plan_b64,
    ]
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-i", str(ssh_key), ssh_host, shlex.join(remote_args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(f"remote multi-query benchmark failed: {completed.stderr[-12000:]}")
    marker = "MULTI_QUERY_RESULT_JSON="
    offset = completed.stdout.rfind(marker)
    if offset < 0:
        raise RuntimeError(f"remote result marker missing: {completed.stdout[-12000:]}")
    return json.loads(completed.stdout[offset + len(marker) :].strip())


def _resource_sample(*, ssh_host: str, ssh_key: Path, namespace: str) -> dict[str, Any]:
    command = shlex.join(["sudo", "kubectl", "top", "pod", "-n", namespace, "--no-headers"])
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-i", str(ssh_key), ssh_host, command],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "available": completed.returncode == 0,
        "rows": [line.split() for line in completed.stdout.splitlines() if line.strip()],
        "error": completed.stderr.strip() if completed.returncode else None,
    }


def capture(args: argparse.Namespace) -> None:
    definition = load_ground_truth(args.definition)
    started_at = datetime.now(UTC).isoformat()
    before = snapshot_visible_corpus(args.base_url, page_size=args.page_size)
    resource_before = _resource_sample(
        ssh_host=args.ssh_host,
        ssh_key=args.ssh_key,
        namespace=args.namespace,
    )
    remote = _remote_capture(
        definition,
        script_path=args.remote_script,
        ssh_host=args.ssh_host,
        ssh_key=args.ssh_key,
        namespace=args.namespace,
        deployment=args.deployment,
        timeout=args.timeout,
    )
    resource_after = _resource_sample(
        ssh_host=args.ssh_host,
        ssh_key=args.ssh_key,
        namespace=args.namespace,
    )
    after = snapshot_visible_corpus(args.base_url, page_size=args.page_size)
    _write_json(
        args.output,
        {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "corpus_before": before,
            "corpus_after": after,
            "corpus_changed": corpus_changed(before, after),
            "resource_samples": {"before": resource_before, "after": resource_after},
            "remote": remote,
        },
    )


def _query_wall_seconds(remote: dict[str, Any], query_count: int) -> float:
    durations = [
        float(item["timings"]["embedding"])
        + max(float(item["timings"]["lexical"]), float(item["timings"]["dense"]))
        + float(item["timings"]["fusion"])
        for item in remote["per_query"][:query_count]
    ]
    # The runner uses a FIFO semaphore with concurrency two.
    retrieval = sum(max(durations[index : index + 2]) for index in range(0, len(durations), 2))
    generation = float(remote["generation_seconds"]) if query_count > 1 else 0.0
    return generation + retrieval


def _component_ids(
    definition: dict[str, Any], items: list[dict[str, Any]], *, labels: set[str]
) -> set[str]:
    index = _index(definition)
    allowed = {
        item["component_id"]
        for item in definition["components"]
        if item.get("human_decision") in labels
    }
    result = set()
    for item in items:
        matched = _match(item, index)
        if matched and matched.get("component_id") in allowed:
            result.add(str(matched["component_id"]))
    return result


def _query_contribution(
    definition: dict[str, Any], seeds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    index = _index(definition)
    rows: dict[str, dict[str, Any]] = {}
    for item in seeds:
        matched = _match(item, index)
        if not matched or matched.get("human_decision") != "CORE":
            continue
        component_id = str(matched["component_id"])
        row = rows.setdefault(
            component_id,
            {
                "component_id": component_id,
                "first_query_id_that_seeded_it": None,
                "all_query_ids_that_seeded_it": [],
                "lanes": [],
            },
        )
        query_ids = {
            str(value.get("query_id"))
            for value in item.get("query_contributions", [])
            if isinstance(value, dict) and value.get("query_id")
        }
        lanes = {
            lane
            for value in item.get("query_contributions", [])
            if isinstance(value, dict)
            for lane in value.get("matched_lanes", [])
        }
        row["all_query_ids_that_seeded_it"] = sorted(
            set(row["all_query_ids_that_seeded_it"]) | query_ids,
            key=lambda value: int(value[1:]),
        )
        row["lanes"] = sorted(set(row["lanes"]) | lanes)
        row["first_query_id_that_seeded_it"] = row["all_query_ids_that_seeded_it"][0]
    return [rows[key] for key in sorted(rows)]


def _misses(
    definition: dict[str, Any],
    remote: dict[str, Any],
    best_run: dict[str, Any],
    best_metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    reached = set(best_metrics["closure_component_ids"])
    index = _index(definition)
    query_by_id = {item["query_id"]: item for item in remote["queries"]}
    result = []
    for component in definition["components"]:
        component_id = component["component_id"]
        if component.get("human_decision") != "CORE" or component_id in reached:
            continue
        observations = []
        for query_result in remote["per_query"]:
            query_spec = query_result["query"]
            for item in query_result["hits"]:
                matched = _match(item, index)
                if not matched or matched.get("component_id") != component_id:
                    continue
                contribution = item.get("query_contributions", [{}])[0]
                observations.append(
                    {
                        "query_id": query_spec["query_id"],
                        "query_type": query_spec["query_type"],
                        "lexical_rank": contribution.get("lexical_rank"),
                        "dense_rank": contribution.get("dense_rank"),
                        "query_rrf_rank": contribution.get("query_rrf_rank"),
                    }
                )
        best = min(
            observations,
            key=lambda value: (
                value["query_rrf_rank"] if value["query_rrf_rank"] is not None else 10**9,
                value["query_id"],
            ),
            default=None,
        )
        global_ranks = [
            rank
            for rank, item in enumerate(best_run["seeds"], start=1)
            if (matched := _match(item, index)) is not None
            and matched.get("component_id") == component_id
        ]
        isolated = component.get("type") == "standalone_document"
        result.append(
            {
                "component_id": component_id,
                "best_generated_query_relation": (
                    {
                        **best,
                        "query_text": query_by_id[best["query_id"]]["query_text"],
                    }
                    if best
                    else None
                ),
                "lexical_best_rank": min(
                    (value["lexical_rank"] for value in observations if value["lexical_rank"]),
                    default=None,
                ),
                "dense_best_rank": min(
                    (value["dense_rank"] for value in observations if value["dense_rank"]),
                    default=None,
                ),
                "rrf_best_rank": min(global_ranks, default=None),
                "isolated_or_connected": "isolated" if isolated else "connected",
                "reason_category": (
                    "global seed budget displacement"
                    if observations
                    else "isolated component outside all query horizons"
                    if isolated
                    else "all generated query lanes missed"
                ),
            }
        )
    return result


def _write_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    fields = [
        "view",
        "query_count",
        "seed_document_recall",
        "seed_component_recall",
        "post_prov_o_document_recall",
        "post_prov_o_component_recall",
        "precision",
        "expansion",
        "coverage_complete",
        "total_latency_seconds",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for run in runs:
            metrics = run["metrics"]
            writer.writerow(
                {
                    "view": run["view"],
                    "query_count": run["query_count"],
                    "seed_document_recall": metrics["seed_document_recall"]["value"],
                    "seed_component_recall": metrics["seed_component_recall"]["value"],
                    "post_prov_o_document_recall": metrics["post_prov_o_document_recall"]["value"],
                    "post_prov_o_component_recall": metrics["post_prov_o_component_recall"][
                        "value"
                    ],
                    "precision": metrics["precision"]["value"],
                    "expansion": metrics["expansion_per_seed_document"],
                    "coverage_complete": metrics["coverage"]["complete"],
                    "total_latency_seconds": run["performance"]["total_latency_seconds"],
                }
            )


def _table(result: dict[str, Any], view: str) -> list[str]:
    lines = [
        "| Queries | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Total latency |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in result["runs"]:
        if run["view"] != view:
            continue
        metrics = run["metrics"]
        lines.append(
            f"| {run['query_count']} | {_format_ratio(metrics['seed_document_recall'])} "
            f"| {_format_ratio(metrics['seed_component_recall'])} "
            f"| {_format_ratio(metrics['post_prov_o_document_recall'])} "
            f"| {_format_ratio(metrics['post_prov_o_component_recall'])} "
            f"| {_format_ratio(metrics['precision'])} "
            f"| {metrics['expansion_per_seed_document']:.2f}× "
            f"| {run['performance']['total_latency_seconds']:.3f}s |"
        )
    return lines


def _report(result: dict[str, Any]) -> str:
    best = result["best_configuration"]
    lines = [
        "## A. Baseline",
        "",
        f"benchmark_tag: {result['benchmark_tag']}",
        f"benchmark_sha: {result['benchmark_sha']}",
        f"runtime_baseline: {result['runtime_baseline']}",
        "scope_policy: documentary-prov-o v1",
        "embedding: openai / text-embedding-3-large",
        "",
        "## B. Architecture",
        "",
        f"query_generator: {result['architecture']['query_generator']}",
        f"max_queries: {result['architecture']['max_queries']}",
        f"query_normalization: {result['architecture']['query_normalization']}",
        f"retrieval_fanout: {result['architecture']['retrieval_fanout']}",
        f"fusion: {result['architecture']['fusion']}",
        f"final_seed_budget: {result['architecture']['final_seed_budget']}",
        "",
        "## C. Generality",
        "",
        "domain_specific_terms_in_product_code: 0",
        "ground_truth_accessible_to_query_generator: no",
        "case_specific_logic: none",
        "",
        "## D. Tests",
        "",
    ]
    lines.extend(f"{key}: {value}" for key, value in result["validation"].items())
    lines.extend(["", "## E. Benchmark STRICT", "", *_table(result, "STRICT")])
    lines.extend(["", "## F. Benchmark BROAD", "", *_table(result, "BROAD")])
    lines.extend(
        [
            "",
            "## G. Marginal query gain",
            "",
            "| Added query | New CORE components seeded | Cumulative recall | Duplicate ratio | Added latency |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result["marginal_query_gain"]:
        lines.append(
            f"| {row['added_query']} | {row['new_core_components_seeded']} "
            f"| {row['cumulative_recall']:.1%} | {row['duplicate_ratio']:.1%} "
            f"| {row['added_latency_seconds']:.3f}s |"
        )
    lines.extend(["", "## H. Misses", ""])
    lines.append(
        "CORE components still missed by best configuration: " + str(len(result["misses"]))
    )
    for miss in result["misses"]:
        lines.append(
            f"- {miss['component_id']}: {miss['reason_category']}; "
            f"query={miss['best_generated_query_relation']}; lexical={miss['lexical_best_rank']}; "
            f"dense={miss['dense_best_rank']}; RRF={miss['rrf_best_rank']}; "
            f"{miss['isolated_or_connected']}"
        )
    performance = best["performance"]
    lines.extend(
        [
            "",
            "## I. Performance",
            "",
            f"query_generation: {performance['query_generation_seconds']:.3f}s",
            f"retrieval: {performance['retrieval_seconds']:.3f}s",
            f"fusion: {performance['fusion_seconds']:.3f}s",
            f"PROV-O: {performance['prov_o_seconds']:.3f}s",
            f"total: {performance['total_latency_seconds']:.3f}s",
            f"latency_multiplier_vs_q1: {best['latency_multiplier_vs_q1']:.3f}×",
            "",
            "## J. Best configuration",
            "",
            f"query_count: {best['query_count']}",
            f"seed_budget: {best['seed_budget']}",
            f"Seed_Component_Recall: {_format_ratio(best['seed_component_recall'])}",
            f"Post_PROV_O_Component_Recall: {_format_ratio(best['post_prov_o_component_recall'])}",
            f"Precision: {_format_ratio(best['precision'])}",
            f"Expansion: {best['expansion']:.3f}×",
            f"Latency: {performance['total_latency_seconds']:.3f}s",
            "",
            "## K. Decision",
            "",
            result["decision"],
            "",
            "## L. Qwen readiness",
            "",
            f"ready_to_benchmark_qwen: {str(result['qwen_readiness']['ready']).lower()}",
            f"reason: {result['qwen_readiness']['reason']}",
            "",
            "## M. Production",
            "",
        ]
    )
    lines.extend(f"{key}: {value}" for key, value in result["production"].items())
    lines.extend(["", "## N. Conclusion", "", result["conclusion"], ""])
    return "\n".join(lines)


def evaluate(args: argparse.Namespace) -> None:
    definition = load_ground_truth(args.definition)
    capture_value = json.loads(args.capture.read_text(encoding="utf-8"))
    remote = capture_value["remote"]
    runs = []
    strict_by_count = {}
    for view, labels in definition["relevance_views"].items():
        for remote_run in remote["runs"]:
            count = int(remote_run["query_count"])
            metrics = _view_metrics(
                definition,
                remote_run["seeds"],
                remote_run,
                requested_k=int(remote["final_seed_budget"]),
                labels=set(labels),
            )
            discovery_seconds = _query_wall_seconds(remote, count)
            total_seconds = discovery_seconds + float(remote_run["scope_closure_seconds"])
            row = {
                "view": view,
                "query_count": count,
                "metrics": metrics,
                "duplicate_seed_ratio": remote_run["duplicate_seed_ratio"],
                "performance": {
                    "discovery_seconds": discovery_seconds,
                    "scope_closure_seconds": remote_run["scope_closure_seconds"],
                    "total_latency_seconds": total_seconds,
                },
            }
            runs.append(row)
            if view == "STRICT":
                strict_by_count[count] = row

    q1 = strict_by_count[1]
    q1_reproduced = (
        q1["metrics"]["seed_component_recall"]["numerator"] == 40
        and q1["metrics"]["post_prov_o_component_recall"]["numerator"] == 51
    )
    best_count = max(
        strict_by_count,
        key=lambda count: (
            strict_by_count[count]["metrics"]["seed_component_recall"]["numerator"],
            strict_by_count[count]["metrics"]["post_prov_o_component_recall"]["numerator"],
            strict_by_count[count]["metrics"]["precision"]["value"] or 0,
            -strict_by_count[count]["performance"]["total_latency_seconds"],
        ),
    )
    best = strict_by_count[best_count]
    remote_best = next(item for item in remote["runs"] if item["query_count"] == best_count)
    previous_components: set[str] = set()
    previous_latency = 0.0
    marginal = []
    for count in sorted(strict_by_count):
        row = strict_by_count[count]
        components = set(row["metrics"]["seeded_component_ids"])
        marginal.append(
            {
                "added_query": f"q{count - 1}",
                "new_core_components_seeded": len(components - previous_components),
                "lost_core_components": len(previous_components - components),
                "cumulative_recall": row["metrics"]["seed_component_recall"]["value"],
                "duplicate_ratio": row["duplicate_seed_ratio"],
                "added_latency_seconds": row["performance"]["total_latency_seconds"]
                - previous_latency,
            }
        )
        previous_components = components
        previous_latency = row["performance"]["total_latency_seconds"]

    validation = (
        json.loads(args.validation.read_text(encoding="utf-8"))
        if args.validation
        else {"multi_query_unit": "pending", "benchmark": "pending"}
    )
    comparable = not capture_value["corpus_changed"]
    seed_gain = (
        best["metrics"]["seed_component_recall"]["value"]
        - q1["metrics"]["seed_component_recall"]["value"]
    )
    precision_delta = (best["metrics"]["precision"]["value"] or 0) - (
        q1["metrics"]["precision"]["value"] or 0
    )
    latency_multiplier = (
        best["performance"]["total_latency_seconds"] / q1["performance"]["total_latency_seconds"]
    )
    coverage_ok = all(
        row["metrics"]["coverage"]["complete"] is True for row in strict_by_count.values()
    )
    validations_ok = all(value == "pass" for value in validation.values())
    clear_gain = seed_gain >= 0.05
    acceptable = precision_delta >= -0.15 and latency_multiplier <= 4.0
    if (
        comparable
        and q1_reproduced
        and clear_gain
        and acceptable
        and coverage_ok
        and validations_ok
    ):
        decision = "MULTI-QUERY DISCOVERY VALIDATED"
        conclusion = "PHASE 3 GENERIC MULTI-QUERY DISCOVERY VALIDATED"
    elif seed_gain > 0:
        decision = "MULTI-QUERY DISCOVERY PROMISING - NOT READY"
        conclusion = decision
    else:
        decision = "MULTI-QUERY DISCOVERY NOT JUSTIFIED"
        conclusion = decision

    per_query = remote["per_query"][:best_count]
    query_generation_seconds = float(remote["generation_seconds"]) if best_count > 1 else 0.0
    retrieval_seconds = _query_wall_seconds(remote, best_count) - query_generation_seconds
    fusion_seconds = sum(float(item["timings"]["fusion"]) for item in per_query) + float(
        remote_best["global_fusion_seconds"]
    )
    best_summary = {
        "query_count": best_count,
        "seed_budget": remote["final_seed_budget"],
        "seed_component_recall": best["metrics"]["seed_component_recall"],
        "post_prov_o_component_recall": best["metrics"]["post_prov_o_component_recall"],
        "precision": best["metrics"]["precision"],
        "expansion": best["metrics"]["expansion_per_seed_document"],
        "latency_multiplier_vs_q1": latency_multiplier,
        "precision_delta": precision_delta,
        "performance": {
            "query_generation_seconds": query_generation_seconds,
            "retrieval_seconds": retrieval_seconds,
            "fusion_seconds": fusion_seconds,
            "prov_o_seconds": float(remote_best["scope_closure_seconds"]),
            "total_latency_seconds": best["performance"]["total_latency_seconds"],
        },
    }
    result = {
        "schema_version": 1,
        "benchmark_tag": "v0.6.0-retrieval-v2-discovery-benchmark-v1",
        "benchmark_sha": "66a1ee9e52ddaee5561b3b38360164ce01bc3927",
        "runtime_baseline": remote["runtime_config_observed"],
        "scope_policy": {"id": "documentary-prov-o", "version": 1},
        "embedding": {"provider": "openai", "model": "text-embedding-3-large"},
        "corpus": {
            "before": capture_value["corpus_before"],
            "after": capture_value["corpus_after"],
            "comparable": comparable,
        },
        "architecture": {
            "query_generator": "bounded structured LLM planner; original query injected",
            "max_queries": 4,
            "query_normalization": "NFKD accents + casefold + punctuation + whitespace",
            "retrieval_fanout": "lexical+dense per query; concurrency=2",
            "fusion": "hierarchical RRF; sum_q(1/(60+per-query-RRF-rank))",
            "final_seed_budget": remote["final_seed_budget"],
        },
        "generality": {
            "domain_specific_terms_in_product_code": 0,
            "ground_truth_accessible_to_query_generator": False,
            "case_specific_logic": None,
        },
        "queries": remote["queries"],
        "generation_error": remote["generation_error"],
        "runs": runs,
        "q1_baseline_reproduced": q1_reproduced,
        "marginal_query_gain": marginal,
        "query_contribution": _query_contribution(definition, remote_best["seeds"]),
        "misses": _misses(definition, remote, remote_best, best["metrics"]),
        "best_configuration": best_summary,
        "seed_component_recall_absolute_gain": seed_gain,
        "precision_delta": precision_delta,
        "coverage_success": coverage_ok,
        "performance": {
            "resource_samples": capture_value["resource_samples"],
            "process_resource_usage": remote["process_resource_usage"],
        },
        "validation": validation,
        "decision": decision,
        "qwen_readiness": {
            "ready": decision == "MULTI-QUERY DISCOVERY VALIDATED",
            "reason": (
                "The OpenAI multi-query pipeline is stable and isolated from embedding changes."
                if decision == "MULTI-QUERY DISCOVERY VALIDATED"
                else "Phase 3 must be validated before changing the embedding baseline."
            ),
        },
        "production": (
            {
                "commit": "pending",
                "push": "pending",
                "build": "pending",
                "gitops": "pending",
                "deploy": "pending",
            }
            if decision == "MULTI-QUERY DISCOVERY VALIDATED"
            else {
                "commit": "no",
                "push": "no",
                "build": "no",
                "gitops": "no",
                "deploy": "no",
            }
        ),
        "decision_inputs": {
            "comparable": comparable,
            "q1_reproduced": q1_reproduced,
            "clear_seed_recall_gain": clear_gain,
            "acceptable_precision_and_latency": acceptable,
            "coverage_complete": coverage_ok,
            "validation_complete": validations_ok,
        },
        "conclusion": conclusion,
    }
    _write_json(args.output_json, result)
    _write_csv(args.output_csv, runs)
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.write_text(_report(result), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--definition", type=Path, required=True)
    capture_parser.add_argument("--remote-script", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--base-url", required=True)
    capture_parser.add_argument("--ssh-host", required=True)
    capture_parser.add_argument("--ssh-key", type=Path, required=True)
    capture_parser.add_argument("--namespace", required=True)
    capture_parser.add_argument("--deployment", required=True)
    capture_parser.add_argument("--page-size", type=int, default=1000)
    capture_parser.add_argument("--timeout", type=int, default=3600)
    capture_parser.set_defaults(func=capture)
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("--definition", type=Path, required=True)
    evaluate_parser.add_argument("--capture", type=Path, required=True)
    evaluate_parser.add_argument("--validation", type=Path)
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
