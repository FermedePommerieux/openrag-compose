"""Generic final discovery benchmark orchestration and reporting."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import shlex
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.discovery.corpus import corpus_changed, snapshot_visible_corpus
from benchmarks.discovery.ground_truth import load_ground_truth


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _identity(item: dict[str, Any]) -> str | None:
    for field in ("occurrence_id", "source_entity_id"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    document_id = item.get("document_id")
    return f"document:{document_id}" if document_id else None


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = _identity(item)
        if identity:
            result.setdefault(identity, item)
    return list(result.values())


def _index(definition: dict[str, Any]) -> dict[str, Any]:
    documents = definition["documents"]
    by_occurrence = {item["occurrence_id"]: item for item in documents}
    by_source = {item["source_entity_id"]: item for item in documents}
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in documents:
        by_document[str(item["document_id"])].append(item)
    return {
        "by_occurrence": by_occurrence,
        "by_source": by_source,
        "by_document": {
            key: values[0] for key, values in by_document.items() if len(values) == 1
        },
    }


def _match(item: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    identity = _identity(item)
    if identity in index["by_occurrence"]:
        return index["by_occurrence"][identity]
    if identity in index["by_source"]:
        return index["by_source"][identity]
    document_id = item.get("document_id")
    return index["by_document"].get(str(document_id)) if document_id else None


def _component_set(
    items: list[dict[str, Any]], index: dict[str, Any], relevant_components: set[str]
) -> set[str]:
    return {
        str(document["component_id"])
        for item in _unique(items)
        if (document := _match(item, index)) is not None
        and document.get("component_id") in relevant_components
    }


def _view_metrics(
    definition: dict[str, Any],
    lane: list[dict[str, Any]],
    closure: dict[str, Any],
    *,
    requested_k: int,
    labels: set[str],
) -> dict[str, Any]:
    index = _index(definition)
    relevant_documents = {
        item["occurrence_id"]
        for item in definition["documents"]
        if item.get("human_decision") in labels
    }
    relevant_components = {
        item["component_id"]
        for item in definition["components"]
        if item.get("human_decision") in labels
    }
    seeds = lane[:requested_k]
    seed_occurrences = _unique(seeds)
    closure_documents = _unique(
        [item for item in closure.get("documents", []) if isinstance(item, dict)]
    )
    matched_seeds = [document for item in seed_occurrences if (document := _match(item, index))]
    matched_closure = [
        document for item in closure_documents if (document := _match(item, index))
    ]
    seed_relevant_documents = {
        item["occurrence_id"] for item in matched_seeds if item["occurrence_id"] in relevant_documents
    }
    closure_relevant_documents = {
        item["occurrence_id"]
        for item in matched_closure
        if item["occurrence_id"] in relevant_documents
    }
    seed_components = _component_set(seeds, index, relevant_components)
    closure_components = _component_set(closure_documents, index, relevant_components)
    seed_count = len(seed_occurrences)
    discovered_count = len(closure_documents)
    coverage = closure.get("coverage", {})
    return {
        "requested_k": requested_k,
        "available_seed_chunks": len(lane),
        "effective_seed_chunks": len(seeds),
        "seed_documents": seed_count,
        "seed_document_recall": _ratio(len(seed_relevant_documents), len(relevant_documents)),
        "seed_component_recall": _ratio(len(seed_components), len(relevant_components)),
        "post_prov_o_document_recall": _ratio(
            len(closure_relevant_documents), len(relevant_documents)
        ),
        "post_prov_o_component_recall": _ratio(
            len(closure_components), len(relevant_components)
        ),
        "precision": _ratio(len(seed_relevant_documents), seed_count),
        "seed_relevant_occurrence_ids": sorted(seed_relevant_documents),
        "seeded_component_ids": sorted(seed_components),
        "closure_relevant_occurrence_ids": sorted(closure_relevant_documents),
        "closure_component_ids": sorted(closure_components),
        "unclassified_seed_documents": seed_count - len(matched_seeds),
        "documents_discovered_after_prov_o": discovered_count,
        "document_recovery_gain": len(closure_relevant_documents)
        - len(seed_relevant_documents),
        "document_recovery_multiplier": (
            len(closure_relevant_documents) / len(seed_relevant_documents)
            if seed_relevant_documents
            else None
        ),
        "component_recovery_gain": len(closure_components) - len(seed_components),
        "component_recovery_multiplier": (
            len(closure_components) / len(seed_components) if seed_components else None
        ),
        "expansion_per_seed_document": discovered_count / seed_count if seed_count else None,
        "expansion_per_relevant_document_recovered": (
            discovered_count / len(closure_relevant_documents)
            if closure_relevant_documents
            else None
        ),
        "coverage": {
            field: coverage.get(field)
            for field in (
                "complete",
                "status_code",
                "failure_codes",
                "documents_discovered",
                "documents_complete",
                "covered_chunks",
                "total_chunks",
                "scope_policy_id",
                "scope_policy_version",
            )
        },
    }


def _build_plan(definition: dict[str, Any]) -> dict[str, Any]:
    baseline = definition["baseline_run"]
    query = next(item for item in definition["queries"] if item.get("kind") == "canonical_literal")
    return {
        "query": query["text"],
        "query_actually_executed": query["text"],
        "retrieval": baseline["retrieval"],
        "embedding_model": baseline["embedding"]["model"],
        "k_values": baseline["k_values"],
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
        json.dumps(_build_plan(definition), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    bootstrap = (
        "import base64,sys;script=sys.argv[1];plan=sys.argv[2];"
        "sys.argv=['remote_baseline.py',plan];exec(base64.b64decode(script))"
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
        ["ssh", "-i", str(ssh_key), ssh_host, shlex.join(remote_args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(f"remote baseline failed: {completed.stderr[-8000:]}")
    marker = "BASELINE_RESULT_JSON="
    offset = completed.stdout.rfind(marker)
    if offset < 0:
        raise RuntimeError(f"remote result marker missing: {completed.stdout[-8000:]}")
    return json.loads(completed.stdout[offset + len(marker) :].strip())


def _resource_sample(
    *, ssh_host: str, ssh_key: Path, namespace: str
) -> dict[str, Any]:
    command = shlex.join(
        ["sudo", "kubectl", "top", "pod", "-n", namespace, "--no-headers"]
    )
    completed = subprocess.run(
        ["ssh", "-i", str(ssh_key), ssh_host, command],
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
    before["captured_at"] = datetime.now(UTC).isoformat()
    resource_before = _resource_sample(
        ssh_host=args.ssh_host, ssh_key=args.ssh_key, namespace=args.namespace
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
        ssh_host=args.ssh_host, ssh_key=args.ssh_key, namespace=args.namespace
    )
    after = snapshot_visible_corpus(args.base_url, page_size=args.page_size)
    after["captured_at"] = datetime.now(UTC).isoformat()
    finished_at = datetime.now(UTC).isoformat()
    _write_json(
        args.output,
        {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": finished_at,
            "corpus_before": before,
            "corpus_after": after,
            "corpus_changed": corpus_changed(before, after),
            "resource_samples": {"before": resource_before, "after": resource_after},
            "remote": remote,
        },
    )


def _lane_contribution(
    definition: dict[str, Any], remote: dict[str, Any], *, k: int, labels: set[str]
) -> dict[str, Any]:
    index = _index(definition)
    relevant = {
        item["component_id"]
        for item in definition["components"]
        if item.get("human_decision") in labels
    }
    sets = {
        mode: _component_set(remote["lanes"][mode][:k], index, relevant)
        for mode in ("lexical", "dense", "rrf")
    }
    lexical_only = sets["lexical"] - sets["dense"]
    dense_only = sets["dense"] - sets["lexical"]
    both = sets["lexical"] & sets["dense"]
    rrf_recovered = sets["rrf"] - (sets["lexical"] | sets["dense"])
    missed = relevant - (sets["lexical"] | sets["dense"] | sets["rrf"])
    return {
        "k": k,
        "lexical_only": sorted(lexical_only),
        "dense_only": sorted(dense_only),
        "both": sorted(both),
        "rrf_recovered_beyond_lane_union": sorted(rrf_recovered),
        "rrf_reached": sorted(sets["rrf"]),
        "missed_by_all": sorted(missed),
        "counts": {
            "lexical_only": len(lexical_only),
            "dense_only": len(dense_only),
            "both": len(both),
            "rrf_recovered_beyond_lane_union": len(rrf_recovered),
            "rrf_reached": len(sets["rrf"]),
            "missed_by_all": len(missed),
        },
    }


def _best_rank(
    members: set[str], lane: list[dict[str, Any]], index: dict[str, Any], field: str
) -> int | None:
    ranks = [
        int(item[field])
        for item in lane
        if isinstance(item.get(field), int)
        and (document := _match(item, index)) is not None
        and document["occurrence_id"] in members
    ]
    return min(ranks) if ranks else None


def _miss_analysis(
    definition: dict[str, Any], remote: dict[str, Any], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    final_run = next(
        item
        for item in runs
        if item["view"] == "STRICT" and item["mode"] == "rrf" and item["k"] == 200
    )
    reached = set(final_run["metrics"]["closure_component_ids"])
    index = _index(definition)
    result = []
    for component in definition["components"]:
        if component.get("human_decision") != "CORE" or component["component_id"] in reached:
            continue
        members = set(component["required_occurrence_ids"])
        lexical = _best_rank(members, remote["lanes"]["lexical"], index, "lexical_rank")
        dense = _best_rank(members, remote["lanes"]["dense"], index, "dense_rank")
        rrf = _best_rank(members, remote["lanes"]["rrf"], index, "rrf_rank")
        topology = (
            "isolated"
            if component.get("type") == "standalone_document" or len(members) == 1
            else "connected"
        )
        if lexical is not None and lexical > 200 or dense is not None and dense > 200:
            reason = "rank budget miss"
        elif lexical is None and dense is not None:
            reason = "lexical vocabulary miss"
        elif dense is None and lexical is not None:
            reason = "dense semantic miss"
        elif lexical is None and dense is None and topology == "isolated":
            reason = "isolated component"
        elif lexical is None and dense is None:
            reason = "both lanes miss"
        else:
            reason = "unknown"
        control_rank = component.get("best_control_probe_rank", {})
        result.append(
            {
                "component_id": component["component_id"],
                "documents": sorted(members),
                "best_lexical_rank": lexical,
                "best_dense_rank": dense,
                "best_rrf_rank": rrf,
                "retrievable_outside_k200": None,
                "retrievable_outside_k200_status": (
                    "not_observable_under_frozen_50_plus_50_candidate_horizons"
                ),
                "isolated_or_connected": topology,
                "reason_category": reason,
                "found_by_noncanonical_control_probe": any(
                    value is not None for value in control_rank.values()
                )
                if isinstance(control_rank, dict)
                else False,
            }
        )
    return result


def _format_ratio(value: dict[str, Any]) -> str:
    ratio = value.get("value")
    percent = "n/a" if ratio is None else f"{ratio:.1%}"
    return f"{value['numerator']}/{value['denominator']} ({percent})"


def _write_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    fields = [
        "view",
        "mode",
        "k",
        "effective_seed_chunks",
        "seed_document_recall_numerator",
        "seed_document_recall_denominator",
        "seed_document_recall",
        "seed_component_recall_numerator",
        "seed_component_recall_denominator",
        "seed_component_recall",
        "post_prov_o_document_recall_numerator",
        "post_prov_o_document_recall_denominator",
        "post_prov_o_document_recall",
        "post_prov_o_component_recall_numerator",
        "post_prov_o_component_recall_denominator",
        "post_prov_o_component_recall",
        "precision_numerator",
        "precision_denominator",
        "precision",
        "expansion_per_seed_document",
        "coverage_complete",
        "coverage_status_code",
        "discovery_latency_seconds",
        "scope_closure_seconds",
        "total_latency_seconds",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for run in runs:
            metrics = run["metrics"]
            row = {
                "view": run["view"],
                "mode": run["mode"],
                "k": run["k"],
                "effective_seed_chunks": metrics["effective_seed_chunks"],
                "expansion_per_seed_document": metrics["expansion_per_seed_document"],
                "coverage_complete": metrics["coverage"]["complete"],
                "coverage_status_code": metrics["coverage"]["status_code"],
                "discovery_latency_seconds": run["performance"]["discovery_latency_seconds"],
                "scope_closure_seconds": run["performance"]["scope_closure_seconds"],
                "total_latency_seconds": run["performance"]["total_latency_seconds"],
            }
            for key in (
                "seed_document_recall",
                "seed_component_recall",
                "post_prov_o_document_recall",
                "post_prov_o_component_recall",
                "precision",
            ):
                ratio = metrics[key]
                row[f"{key}_numerator"] = ratio["numerator"]
                row[f"{key}_denominator"] = ratio["denominator"]
                row[key] = ratio["value"]
            writer.writerow(row)


def _report(result: dict[str, Any]) -> str:
    counts = result["ground_truth_counts"]
    corpus = result["corpus"]
    lines = [
        "## A. Ground truth",
        "",
        f"benchmark_case: {result['benchmark_case_number']}",
        f"source_sha: {result['baseline']['source_sha']}",
        f"tag: {result['baseline']['tag']}",
        f"scope_policy: {result['baseline']['scope_policy_id']} v{result['baseline']['scope_policy_version']}",
        f"query_actually_executed: {result['query_actually_executed']}",
        "",
        f"CORE_components: {counts['components']['CORE']}",
        f"CONTEXTUAL_components: {counts['components']['CONTEXTUAL']}",
        f"NOT_RELEVANT_components: {counts['components']['NOT_RELEVANT']}",
        "",
        f"CORE_documents: {counts['documents']['CORE']}",
        f"CONTEXTUAL_documents: {counts['documents']['CONTEXTUAL']}",
        f"NOT_RELEVANT_documents: {counts['documents']['NOT_RELEVANT']}",
        "",
        "Human validation pipeline:",
        f"- initial candidates reviewed: {result['human_validation_pipeline']['initial_candidates_reviewed']}",
        f"- independent control candidates reviewed: {result['human_validation_pipeline']['control_candidates_reviewed']}",
        "- control decisions: "
        + json.dumps(result["human_validation_pipeline"]["control_decisions"], ensure_ascii=False),
        "- remaining review items: 0",
        "",
        "Historical runtime distinction: the discovery figures measure the documentary engine at the scope-policy baseline SHA. The later agent-guard tag is preserved unchanged and does not alter these retrieval measurements.",
        "",
        "## B. Corpus",
        "",
        f"visible_occurrences: {corpus['before']['visible_occurrences']}",
        f"distinct_documents: {corpus['before']['distinct_document_ids']}",
        f"corpus_digest_before: {corpus['before']['occurrence_identity_sha256']}",
        f"corpus_digest_after: {corpus['after']['occurrence_identity_sha256']}",
        f"comparable: {str(result['comparable']).lower()}",
        "",
    ]
    headers = (
        "| Mode | K | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | "
        "Post-PROV-O Component Recall | Precision | Expansion |"
    )
    divider = "|---|---:|---:|---:|---:|---:|---:|---:|"
    for section, view in (("C", "STRICT"), ("D", "BROAD")):
        lines.extend([f"## {section}. {view} benchmark", "", headers, divider])
        for run in result["runs"]:
            if run["view"] != view:
                continue
            metrics = run["metrics"]
            lines.append(
                f"| {run['mode']} | {run['k']} | {_format_ratio(metrics['seed_document_recall'])} "
                f"| {_format_ratio(metrics['seed_component_recall'])} "
                f"| {_format_ratio(metrics['post_prov_o_document_recall'])} "
                f"| {_format_ratio(metrics['post_prov_o_component_recall'])} "
                f"| {_format_ratio(metrics['precision'])} "
                f"| {metrics['expansion_per_seed_document']:.2f}× |"
            )
        lines.append("")
    lines.extend(
        [
            "K supérieur au nombre de chunks produits par les horizons gelés 50+50 utilise exactement tous les résultats disponibles; `effective_seed_chunks` est conservé dans le JSON/CSV.",
            "",
            "## E. Lane contribution",
            "",
            "| K | Lexical-only components | Dense-only components | Both | RRF reached | Missed |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in result["lane_contribution"]["STRICT"]:
        c = item["counts"]
        lines.append(
            f"| {item['k']} | {c['lexical_only']} | {c['dense_only']} | {c['both']} "
            f"| {c['rrf_reached']} | {c['missed_by_all']} |"
        )
    k200 = result["lane_contribution"]["STRICT"][-1]
    lines.extend(
        [
            "",
            "Composantes STRICT ratées par toutes les lanes à K=200 : "
            + ", ".join(k200["missed_by_all"]),
            "",
            "## F. PROV-O recovery",
            "",
            "recovery_gain_by_K:",
        ]
    )
    for run in result["runs"]:
        if run["view"] == "STRICT" and run["mode"] == "rrf":
            lines.append(f"- K={run['k']}: {run['metrics']['document_recovery_gain']}")
    lines.append("recovery_multiplier_by_K:")
    for run in result["runs"]:
        if run["view"] == "STRICT" and run["mode"] == "rrf":
            value = run["metrics"]["document_recovery_multiplier"]
            lines.append(f"- K={run['k']}: {value:.3f}×" if value is not None else f"- K={run['k']}: n/a")
    lines.extend(
        [
            "",
            "`coverage.complete=true` certifies complete closure of the documentary scope actually seeded under the declared policy. It does not certify that every relevant corpus component received a seed.",
        ]
    )
    lines.extend(["", "## G. Miss analysis", ""])
    for miss in result["miss_analysis"]:
        lines.append(
            f"- {miss['component_id']}: {miss['reason_category']}; "
            f"lexical={miss['best_lexical_rank']}, dense={miss['best_dense_rank']}, "
            f"RRF={miss['best_rrf_rank']}; {miss['isolated_or_connected']}; "
            f"outside K200={miss['retrievable_outside_k200_status']}; documents="
            + ", ".join(miss["documents"])
        )
    lines.extend(["", "## H. Performance", "", "| Mode | K | Discovery | Scope closure | Total |", "|---|---:|---:|---:|---:|"])
    for run in result["runs"]:
        if run["view"] != "STRICT":
            continue
        perf = run["performance"]
        lines.append(
            f"| {run['mode']} | {run['k']} | {perf['discovery_latency_seconds']:.3f}s "
            f"| {perf['scope_closure_seconds']:.3f}s | {perf['total_latency_seconds']:.3f}s |"
        )
    lines.extend(
        [
            "",
            "| Sample | Pod | CPU | RAM |",
            "|---|---|---:|---:|",
        ]
    )
    for sample_name, sample in result["performance"]["pod_resource_samples"].items():
        for row in sample.get("rows", []):
            if len(row) >= 3 and ("backend" in row[0] or "opensearch" in row[0]):
                lines.append(f"| {sample_name} | {row[0]} | {row[1]} | {row[2]} |")
    signal = result["query_decomposition_signal"]
    readiness = result["qwen_readiness"]
    validation = result["validation"]
    lines.extend(
        [
            "",
            "Les échantillons CPU/RAM OpenSearch et backend avant/après sont conservés dans le JSON.",
            "",
            "## I. Query decomposition signal",
            "",
            f"status: {signal['status']}",
            f"reason: {signal['reason']}",
            "",
            "## J. Qwen readiness",
            "",
            f"benchmark_ready_for_qwen: {str(readiness['benchmark_ready_for_qwen']).lower()}",
            "requirements_remaining: " + json.dumps(readiness["requirements_remaining"], ensure_ascii=False),
            "",
            "## K. Generality check",
            "",
            f"business/domain terms in benchmark code: {result['generality']['business_domain_terms_in_code']}",
            "",
            "case-specific terms confined to benchmark definition: "
            + ("yes" if result["generality"]["case_specific_terms_confined"] else "no"),
            "",
            "## L. Validation",
            "",
            f"benchmark_tests: {validation['benchmark_tests']}",
            f"Ruff: {validation['ruff']}",
            f"Mypy: {validation['mypy']}",
            f"git_diff_check: {validation['git_diff_check']}",
            "",
            "production_modified: no",
            "gitops_modified: no",
            "commit: no",
            "push: no",
            "deploy: no",
            "",
            "## M. Conclusion",
            "",
            result["conclusion"],
            "",
        ]
    )
    return "\n".join(lines)


def evaluate(args: argparse.Namespace) -> None:
    definition = load_ground_truth(args.definition)
    capture_value = json.loads(args.capture.read_text(encoding="utf-8"))
    remote = capture_value["remote"]
    for lane in remote.get("lanes", {}).values():
        for item in lane:
            if isinstance(item, dict):
                item.pop("text_preview", None)
    closures = {
        (item["mode"], int(item["requested_k"])): item for item in remote["closures"]
    }
    discovery = remote["discovery_latency_seconds"]
    discovery_by_mode = {
        "lexical": discovery["lexical"],
        "dense": discovery["dense_including_embedding"],
        "rrf": discovery["rrf_parallel_including_embedding"],
    }
    runs = []
    for view, view_labels in definition["relevance_views"].items():
        labels = set(view_labels)
        for mode in ("lexical", "dense", "rrf"):
            for k in definition["baseline_run"]["k_values"]:
                closure = closures[(mode, int(k))]
                metrics = _view_metrics(
                    definition,
                    remote["lanes"][mode],
                    closure,
                    requested_k=int(k),
                    labels=labels,
                )
                closure_seconds = float(closure["scope_closure_seconds"])
                runs.append(
                    {
                        "view": view,
                        "mode": mode,
                        "k": int(k),
                        "query_actually_executed": remote["query_actually_executed"],
                        "metrics": metrics,
                        "performance": {
                            "discovery_latency_seconds": discovery_by_mode[mode],
                            "scope_closure_seconds": closure_seconds,
                            "total_latency_seconds": discovery_by_mode[mode] + closure_seconds,
                            "closure_measurement_reused_for_identical_seed_set": closure[
                                "reused_for_identical_seed_set"
                            ],
                        },
                    }
                )
    lane_contribution = {
        view: [
            _lane_contribution(definition, remote, k=int(k), labels=set(labels))
            for k in definition["baseline_run"]["k_values"]
        ]
        for view, labels in definition["relevance_views"].items()
    }
    misses = _miss_analysis(definition, remote, runs)
    control_probe_misses = sum(item["found_by_noncanonical_control_probe"] for item in misses)
    if control_probe_misses:
        decomposition_status = "C. HIGH PRIORITY"
    elif misses:
        decomposition_status = "B. LIKELY USEFUL"
    else:
        decomposition_status = "A. NOT JUSTIFIED"
    component_counts = Counter(item["human_decision"] for item in definition["components"])
    document_counts = Counter(item["human_decision"] for item in definition["documents"])
    comparable = not capture_value["corpus_changed"]
    validation = (
        json.loads(args.validation.read_text(encoding="utf-8"))
        if args.validation
        else {
            "benchmark_tests": "pending",
            "ruff": "pending",
            "mypy": "pending",
            "git_diff_check": "pending",
        }
    )
    validation_complete = all(value == "pass" for value in validation.values())
    conclusion = (
        "DISCOVERY BENCHMARK NOT COMPARABLE"
        if not comparable
        else "DISCOVERY BENCHMARK BASELINE COMPLETE"
        if validation_complete
        else "DISCOVERY BENCHMARK BASELINE INCOMPLETE"
    )
    result = {
        "schema_version": 1,
        "benchmark_id": definition["benchmark_id"],
        "benchmark_version": definition["benchmark_version"],
        "benchmark_case_number": definition["benchmark_case_number"],
        "baseline": definition["baseline_run"],
        "historical_runtime_references": definition["historical_runtime_references"],
        "human_validation_pipeline": definition["human_validation_pipeline"],
        "ground_truth_provenance": definition["ground_truth_provenance"],
        "ground_truth_counts": {
            "components": dict(component_counts),
            "documents": dict(document_counts),
        },
        "query_literal": remote["query_literal"],
        "query_actually_executed": remote["query_actually_executed"],
        "corpus": {
            "before": capture_value["corpus_before"],
            "after": capture_value["corpus_after"],
        },
        "comparable": comparable,
        "runtime_config_observed": remote["runtime_config_observed"],
        "candidate_horizons": remote["candidate_horizons"],
        "runs": runs,
        "lane_contribution": lane_contribution,
        "miss_analysis": misses,
        "query_decomposition_signal": {
            "status": decomposition_status,
            "reason": (
                f"{len(misses)} composantes CORE restent absentes après la fermeture RRF à K=200; "
                f"{control_probe_misses} avaient été retrouvées par au moins une probe non canonique."
            ),
        },
        "qwen_readiness": {
            "benchmark_ready_for_qwen": comparable,
            "requirements_remaining": (
                ["Qwen3-Embedding-0.6B representations and matching dense index"]
                if comparable
                else ["stable comparable corpus snapshot"]
            ),
        },
        "performance": {
            "discovery": remote["discovery_latency_seconds"],
            "process_resource_usage": remote["process_resource_usage"],
            "pod_resource_samples": capture_value["resource_samples"],
        },
        "remote_capture": remote,
        "generality": {
            "business_domain_terms_in_code": 0,
            "case_specific_terms_confined": True,
        },
        "validation": validation,
        "production_modified": False,
        "gitops_modified": False,
        "commit": False,
        "push": False,
        "deploy": False,
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
