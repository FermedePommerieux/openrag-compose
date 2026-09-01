"""Build an unlabeled, cross-domain human-review universe from read-only lanes.

Retrieval output is discovery evidence only.  This module deliberately emits
empty human labels and cannot create a ground-truth definition.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import re
import shlex
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ALLOWED_HUMAN_LABELS = ("CORE", "CONTEXTUAL", "NOT_RELEVANT")
DOCUMENT_FIELDS = (
    "review_state",
    "human_label",
    "review_notes",
    "review_priority",
    "candidate_id",
    "component_id",
    "occurrence_id",
    "document_id",
    "source_entity_id",
    "title",
    "date",
    "source",
    "sender",
    "recipients",
    "current_closure_member",
    "discovery_reasons",
    "retrieval_probes",
    "best_lexical_rank",
    "best_dense_rank",
    "best_rrf_rank",
    "relations",
    "text_preview",
)
COMPONENT_FIELDS = (
    "review_state",
    "human_label",
    "review_notes",
    "review_priority",
    "component_id",
    "component_key",
    "member_count",
    "member_occurrence_ids",
    "current_closure_members",
    "selection_reason",
    "discovery_reasons",
    "retrieval_probes",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)] if value not in (None, "") else []


def _identity(item: dict[str, Any]) -> str:
    return str(
        item.get("occurrence_id") or item.get("source_entity_id") or item.get("document_id") or ""
    )


def _provenance(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("source_provenance")
    return value if isinstance(value, dict) else {}


def _entity(item: dict[str, Any]) -> dict[str, Any]:
    value = _provenance(item).get("entity")
    return value if isinstance(value, dict) else {}


def _relations(item: dict[str, Any]) -> list[dict[str, Any]]:
    value = _provenance(item).get("relations")
    return (
        [relation for relation in value if isinstance(relation, dict)]
        if isinstance(value, list)
        else []
    )


def _relation_targets(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    values: set[tuple[str, str, str]] = set()
    for relation in _relations(item):
        target = relation.get("target")
        if not isinstance(target, dict) or not target.get("id"):
            continue
        values.add(
            (
                str(relation.get("role") or ""),
                str(target["id"]),
                str(target.get("type") or ""),
            )
        )
    roles = _as_strings(item.get("source_relation_roles"))
    for index, target in enumerate(_as_strings(item.get("source_relation_target_ids"))):
        values.add((roles[index] if index < len(roles) else "", target, ""))
    return sorted(values)


def _component_key(item: dict[str, Any]) -> str:
    targets = _relation_targets(item)
    thread_ids = [
        target_id
        for _role, target_id, target_type in targets
        if target_type == "email_thread" or ":thread:" in target_id
    ]
    if thread_ids:
        return f"thread:{sorted(thread_ids)[0]}"
    linked_ids = [
        target_id
        for role, target_id, _target_type in targets
        if role in {"attachment_of", "reply_to", "references"}
    ]
    if linked_ids:
        return f"linked:{sorted(linked_ids)[0]}"
    return f"occurrence:{_identity(item)}"


def _compact_text(value: Any, limit: int = 700) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit].rstrip()


def _email_header(text: Any, name: str) -> str:
    if not isinstance(text, str):
        return ""
    match = re.search(rf"(?im)^{re.escape(name)}:\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def _rank(capture: dict[str, Any], item: dict[str, Any]) -> int | None:
    lane = str(capture.get("lane") or "")
    for field in (f"{lane}_occurrence_rank", f"{lane}_rank"):
        value = item.get(field)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _minimum(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def load_review_spec(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("review spec must be a schema_version=1 object")
    for field in ("review_id", "domain", "canonical_query"):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"review spec requires {field}")
    queries = value.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("review spec requires queries")
    query_ids: set[str] = set()
    for query in queries:
        if not isinstance(query, dict):
            raise ValueError("review queries must be objects")
        query_id = str(query.get("query_id") or "")
        if not query_id or query_id in query_ids or not str(query.get("text") or ""):
            raise ValueError(f"invalid or duplicate review query: {query_id}")
        query_ids.add(query_id)
        if query.get("candidate_class") not in {
            "canonical",
            "alternate",
            "entity",
            "control",
        }:
            raise ValueError(f"invalid candidate_class for {query_id}")
    return value


def build_remote_plan(spec: dict[str, Any]) -> dict[str, Any]:
    parameters = spec.get("lane_parameters")
    if not isinstance(parameters, dict):
        raise ValueError("review spec requires lane_parameters")
    return {**parameters, "queries": spec["queries"]}


def build_metadata_recovery_plan(
    spec: dict[str, Any], initial_capture: dict[str, Any]
) -> dict[str, Any] | None:
    recovery = spec.get("metadata_recovery")
    if not isinstance(recovery, dict) or recovery.get("enabled") is not True:
        return None
    target_types = {str(value) for value in recovery.get("relation_target_types", []) if value}
    anchors = sorted(
        {
            target_id
            for capture in initial_capture.get("captures", [])
            if isinstance(capture, dict)
            for item in capture.get("results", [])
            if isinstance(item, dict)
            for _role, target_id, target_type in _relation_targets(item)
            if target_type in target_types
        }
    )
    maximum = max(1, int(recovery.get("max_anchors", 5_000)))
    if not anchors:
        return None
    return {
        **spec["lane_parameters"],
        "queries": [],
        "metadata_anchors": anchors[:maximum],
        "metadata_size": max(1, int(recovery.get("review_horizon", 10_000))),
    }


def _probe_specifications(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(query["query_id"]): query for query in spec["queries"]}


def _priority(hits: list[dict[str, Any]], current_closure_member: bool) -> str:
    if any(hit["candidate_class"] == "control" for hit in hits):
        return "control_candidate"
    if not current_closure_member:
        return "outside_baseline_candidate"
    if any(
        hit["candidate_class"] == "canonical"
        and hit["lane"] in {"lexical", "dense", "rrf"}
        and (hit["rank"] or 10**9) <= 50
        for hit in hits
    ):
        return "high_confidence_candidate"
    return "uncertain_candidate"


def build_review_universe(
    spec: dict[str, Any],
    captures: list[dict[str, Any]],
    *,
    baseline_occurrences: set[str] | None = None,
) -> dict[str, Any]:
    """Aggregate evidence without deriving or proposing relevance labels."""

    baseline = baseline_occurrences or set()
    query_specs = _probe_specifications(spec)
    candidates: dict[str, dict[str, Any]] = {}
    for capture_bundle in captures:
        for capture in capture_bundle.get("captures", []):
            if not isinstance(capture, dict):
                continue
            query_id = str(capture.get("query_id") or "")
            query_spec = query_specs.get(query_id, {})
            lane = str(capture.get("lane") or "")
            for item in capture.get("results", []):
                if not isinstance(item, dict):
                    continue
                occurrence = _identity(item)
                if not occurrence:
                    continue
                entry = candidates.setdefault(
                    occurrence,
                    {"item": item, "hits": []},
                )
                if len(str(item.get("text") or "")) > len(str(entry["item"].get("text") or "")):
                    entry["item"] = item
                entry["hits"].append(
                    {
                        "query_id": query_id,
                        "query_text": str(capture.get("text") or ""),
                        "candidate_class": str(
                            query_spec.get("candidate_class") or "metadata_recovery"
                        ),
                        "lane": lane,
                        "rank": _rank(capture, item),
                    }
                )

    documents: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for occurrence, entry in candidates.items():
        item = entry["item"]
        hits = sorted(
            entry["hits"],
            key=lambda hit: (
                hit["query_id"],
                hit["lane"],
                hit["rank"] if hit["rank"] is not None else 10**9,
            ),
        )
        component_key = _component_key(item)
        current_member = occurrence in baseline
        entity = _entity(item)
        text = item.get("text")
        probe_labels = sorted(
            {f"{hit['query_id']}:{hit['lane']}@{hit['rank'] or '-'}" for hit in hits}
        )
        reasons = sorted(
            {
                hit["candidate_class"]
                if hit["candidate_class"] == "metadata_recovery"
                else f"{hit['candidate_class']} query"
                for hit in hits
            }
        )
        relation_labels = [
            f"{role}->{target_id} ({target_type})"
            for role, target_id, target_type in _relation_targets(item)
        ]
        document = {
            "review_state": "unreviewed",
            "human_label": "",
            "review_notes": "",
            "review_priority": _priority(hits, current_member),
            "candidate_id": _stable_id("candidate", occurrence),
            "component_id": _stable_id("component", component_key),
            "component_key": component_key,
            "occurrence_id": occurrence,
            "document_id": str(item.get("document_id") or ""),
            "source_entity_id": str(item.get("source_entity_id") or ""),
            "source_entity_alternate_ids": _as_strings(item.get("source_entity_alternate_ids")),
            "title": str(entity.get("label") or item.get("filename") or ""),
            "date": str(
                entity.get("generated_at_time")
                or item.get("generated_at_time")
                or _email_header(text, "Date")
                or ""
            ),
            "source": str(item.get("source_entity_system") or item.get("connector_type") or ""),
            "sender": _email_header(text, "From"),
            "recipients": "; ".join(
                value for value in (_email_header(text, "To"), _email_header(text, "Cc")) if value
            ),
            "current_closure_member": current_member,
            "discovery_reasons": reasons,
            "retrieval_probes": probe_labels,
            "discovery_query_ids": sorted(
                {hit["query_id"] for hit in hits if hit["query_id"] in query_specs}
            ),
            "best_lexical_rank": _minimum(
                [hit["rank"] for hit in hits if hit["lane"] == "lexical"]
            ),
            "best_dense_rank": _minimum([hit["rank"] for hit in hits if hit["lane"] == "dense"]),
            "best_rrf_rank": _minimum([hit["rank"] for hit in hits if hit["lane"] == "rrf"]),
            "relations": relation_labels,
            "text_preview": _compact_text(text),
        }
        documents.append(document)
        grouped[component_key].append(document)

    priority_order = {
        "outside_baseline_candidate": 0,
        "high_confidence_candidate": 1,
        "uncertain_candidate": 2,
        "control_candidate": 3,
    }
    documents.sort(
        key=lambda row: (
            priority_order[row["review_priority"]],
            row["component_id"],
            row["date"],
            row["occurrence_id"],
        )
    )
    components = []
    for component_key, members in sorted(grouped.items()):
        priorities = sorted(
            {member["review_priority"] for member in members},
            key=priority_order.__getitem__,
        )
        components.append(
            {
                "review_state": "unreviewed",
                "human_label": "",
                "review_notes": "",
                "review_priority": priorities[0],
                "component_id": _stable_id("component", component_key),
                "component_key": component_key,
                "member_count": len(members),
                "member_occurrence_ids": sorted(member["occurrence_id"] for member in members),
                "current_closure_members": sum(
                    member["current_closure_member"] for member in members
                ),
                "discovery_query_ids": sorted(
                    {query_id for member in members for query_id in member["discovery_query_ids"]}
                ),
                "best_rank": _minimum(
                    [
                        member[field]
                        for member in members
                        for field in (
                            "best_lexical_rank",
                            "best_dense_rank",
                            "best_rrf_rank",
                        )
                    ]
                ),
                "discovery_reasons": sorted(
                    {reason for member in members for reason in member["discovery_reasons"]}
                ),
                "retrieval_probes": sorted(
                    {probe for member in members for probe in member["retrieval_probes"]}
                ),
            }
        )
    components.sort(
        key=lambda row: (
            priority_order[row["review_priority"]],
            row["component_id"],
        )
    )
    captured_document_count = len(documents)
    captured_component_count = len(components)
    selection = spec.get("review_selection")
    selection = selection if isinstance(selection, dict) else {}
    maximum_components = max(1, int(selection.get("max_components", captured_component_count or 1)))
    required_query_ids = {str(value) for value in selection.get("required_query_ids", []) if value}
    minimum_queries = max(1, int(selection.get("minimum_independent_queries", 1)))
    maximum_controls = max(0, int(selection.get("max_control_components", 10**9)))
    include_metadata_only = selection.get("include_metadata_only") is True
    for component in components:
        query_ids = set(component["discovery_query_ids"])
        if query_ids & required_query_ids:
            component["selection_reason"] = "required_query_intersection"
        elif component["current_closure_members"]:
            component["selection_reason"] = "current_closure_intersection"
        elif len(query_ids) >= minimum_queries:
            component["selection_reason"] = f"independent_queries>={minimum_queries}"
        elif not query_ids and include_metadata_only:
            component["selection_reason"] = "metadata_recovery_capture"
        elif component["review_priority"] == "control_candidate":
            component["selection_reason"] = "outside_closure_control"
        else:
            component["selection_reason"] = "deferred_single_probe"
    eligible = [
        component
        for component in components
        if component["selection_reason"] != "deferred_single_probe"
    ]
    controls = [
        component
        for component in eligible
        if component["selection_reason"] == "outside_closure_control"
    ][:maximum_controls]
    non_controls = [
        component
        for component in eligible
        if component["selection_reason"] != "outside_closure_control"
    ]
    ranked = sorted(
        [*non_controls, *controls],
        key=lambda component: (
            component["selection_reason"] != "required_query_intersection",
            -len(component["discovery_query_ids"]),
            not bool(component["current_closure_members"]),
            component["best_rank"] or 10**9,
            priority_order[component["review_priority"]],
            component["component_id"],
        ),
    )
    components = ranked[:maximum_components]
    selected_component_ids = {component["component_id"] for component in components}
    documents = [
        document for document in documents if document["component_id"] in selected_component_ids
    ]
    return {
        "schema_version": 1,
        "artifact_type": "unlabeled_human_review_universe",
        "review_builder_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "review_spec_sha256": _sha256_bytes(
            yaml.safe_dump(spec, sort_keys=True, allow_unicode=True).encode("utf-8")
        ),
        "review_id": spec["review_id"],
        "domain": spec["domain"],
        "canonical_query": spec["canonical_query"],
        "human_review_complete": False,
        "allowed_human_labels": list(ALLOWED_HUMAN_LABELS),
        "label_policy": (
            "Retrieval evidence discovers candidates only; every human_label is empty "
            "until a human reviewer decides it."
        ),
        "candidate_count": len(documents),
        "component_count": len(components),
        "captured_candidate_count": captured_document_count,
        "captured_component_count": captured_component_count,
        "deferred_candidate_count": captured_document_count - len(documents),
        "deferred_component_count": captured_component_count - len(components),
        "review_selection": {
            "max_components": maximum_components,
            "required_query_ids": sorted(required_query_ids),
            "minimum_independent_queries": minimum_queries,
            "max_control_components": maximum_controls,
            "include_metadata_only": include_metadata_only,
            "semantics": (
                "Review-capacity prioritization only; deferred raw candidates are neither "
                "relevant nor irrelevant and remain completeness-control inputs."
            ),
        },
        "baseline_occurrence_count": len(baseline),
        "priority_counts": {
            priority: sum(row["review_priority"] == priority for row in documents)
            for priority in priority_order
        },
        "components": components,
        "documents": documents,
    }


def baseline_occurrences(capture: dict[str, Any], experiment_id: str) -> set[str]:
    experiments = capture.get("result", {}).get("sensitivity_experiments", [])
    selected = next(
        (
            item
            for item in experiments
            if isinstance(item, dict) and item.get("experiment_id") == experiment_id
        ),
        None,
    )
    if selected is None:
        raise ValueError(f"baseline experiment not found: {experiment_id}")
    return {
        _identity(item)
        for item in selected.get("documents", [])
        if isinstance(item, dict) and _identity(item)
    }


def build_compact_candidate_capture(
    spec: dict[str, Any],
    captures: list[dict[str, Any]],
    *,
    baseline_occurrences: set[str] | None = None,
) -> dict[str, Any]:
    """Deduplicate raw lane output while retaining auditable discovery evidence."""

    unbounded_spec = {
        **spec,
        "review_selection": {
            "max_components": 100_000,
            "minimum_independent_queries": 1,
            "max_control_components": 100_000,
            "include_metadata_only": True,
        },
    }
    universe = build_review_universe(
        unbounded_spec,
        captures,
        baseline_occurrences=baseline_occurrences,
    )
    documents = []
    for row in universe["documents"]:
        relations = list(row.get("relations", []))
        text_preview = str(row.get("text_preview") or "")
        documents.append(
            {
                key: row[key]
                for key in (
                    "review_state",
                    "human_label",
                    "review_notes",
                    "candidate_id",
                    "component_id",
                    "occurrence_id",
                    "document_id",
                    "source_entity_id",
                    "title",
                    "date",
                    "source",
                    "current_closure_member",
                    "discovery_reasons",
                    "discovery_query_ids",
                    "retrieval_probes",
                    "best_lexical_rank",
                    "best_dense_rank",
                    "best_rrf_rank",
                )
            }
            | {
                "relation_count": len(relations),
                "relations_sha256": _sha256_bytes(
                    json.dumps(
                        relations,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                "text_preview_sha256": _sha256_bytes(text_preview.encode("utf-8")),
            }
        )
    lane_summaries = []
    for bundle_index, bundle in enumerate(captures, start=1):
        for capture in bundle.get("captures", []):
            if not isinstance(capture, dict):
                continue
            occurrences = sorted(
                {
                    _identity(item)
                    for item in capture.get("results", [])
                    if isinstance(item, dict) and _identity(item)
                }
            )
            lane_summaries.append(
                {
                    "bundle": bundle_index,
                    "query_id": capture.get("query_id"),
                    "lane": capture.get("lane"),
                    "raw_chunk_hits": capture.get("raw_chunk_hits"),
                    "returned_occurrences": len(occurrences),
                    "occurrence_set_sha256": _sha256_bytes(
                        json.dumps(occurrences, separators=(",", ":")).encode("utf-8")
                    ),
                }
            )
    return {
        "schema_version": 1,
        "artifact_type": "compact_unlabeled_candidate_capture",
        "review_id": spec["review_id"],
        "review_builder_sha256": universe["review_builder_sha256"],
        "review_spec_sha256": universe["review_spec_sha256"],
        "human_review_complete": False,
        "label_policy": universe["label_policy"],
        "candidate_count": len(documents),
        "component_count": len(universe["components"]),
        "candidate_identity_sha256": _sha256_bytes(
            json.dumps(
                sorted(row["occurrence_id"] for row in documents),
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "lane_summaries": lane_summaries,
        "components": universe["components"],
        "documents": documents,
    }


def capture_remote_plan(
    plan: dict[str, Any],
    *,
    script_path: Path,
    ssh_host: str,
    ssh_key: Path | None,
    namespace: str,
    deployment: str,
    timeout: int,
) -> dict[str, Any]:
    script = script_path.read_bytes()
    script_b64 = base64.b64encode(script).decode("ascii")
    plan_b64 = base64.b64encode(json.dumps(plan, ensure_ascii=False).encode("utf-8")).decode(
        "ascii"
    )
    bootstrap = (
        "import base64,sys;script=sys.argv[1];plan=sys.argv[2];"
        "sys.argv=['remote_lanes.py',plan];exec(base64.b64decode(script))"
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
    ssh_args = ["ssh"]
    if ssh_key is not None:
        ssh_args.extend(["-i", str(ssh_key)])
    completed = subprocess.run(  # noqa: S603 - operator-supplied SSH target
        [*ssh_args, ssh_host, shlex.join(remote_args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(
            f"remote capture failed ({completed.returncode}): {completed.stderr[-4000:]}"
        )
    marker = "CONTROL_RESULT_JSON="
    marker_at = completed.stdout.rfind(marker)
    if marker_at < 0:
        raise RuntimeError(f"remote capture marker missing: {completed.stdout[-4000:]}")
    return json.loads(completed.stdout[marker_at + len(marker) :].strip())


def capture_review_lanes(
    spec: dict[str, Any],
    *,
    script_path: Path,
    ssh_host: str,
    ssh_key: Path | None,
    namespace: str,
    deployment: str,
    timeout: int,
) -> dict[str, Any]:
    initial_plan = build_remote_plan(spec)
    initial = capture_remote_plan(
        initial_plan,
        script_path=script_path,
        ssh_host=ssh_host,
        ssh_key=ssh_key,
        namespace=namespace,
        deployment=deployment,
        timeout=timeout,
    )
    recovery_plan = build_metadata_recovery_plan(spec, initial)
    captures = [initial]
    if recovery_plan is not None:
        anchors = list(recovery_plan["metadata_anchors"])
        recovery = spec.get("metadata_recovery", {})
        batch_size = max(1, int(recovery.get("anchor_batch_size", 100)))
        for offset in range(0, len(anchors), batch_size):
            batch_plan = {
                **recovery_plan,
                "metadata_anchors": anchors[offset : offset + batch_size],
            }
            captures.append(
                capture_remote_plan(
                    batch_plan,
                    script_path=script_path,
                    ssh_host=ssh_host,
                    ssh_key=ssh_key,
                    namespace=namespace,
                    deployment=deployment,
                    timeout=timeout,
                )
            )
    return {
        "schema_version": 1,
        "artifact_type": "unlabeled_candidate_lane_capture",
        "review_id": spec["review_id"],
        "captured_at": datetime.now(UTC).isoformat(),
        "application_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "benchmark_tool_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "remote_script_sha256": _sha256_bytes(script_path.read_bytes()),
        "review_spec_sha256": _sha256_bytes(
            yaml.safe_dump(spec, sort_keys=True, allow_unicode=True).encode("utf-8")
        ),
        "evidence_context": spec.get("evidence_context", {}),
        "initial_plan": initial_plan,
        "metadata_recovery_plan": recovery_plan,
        "metadata_recovery_batches": max(0, len(captures) - 1),
        "captures": captures,
    }


def write_review_artifacts(
    universe: dict[str, Any],
    *,
    json_path: Path,
    documents_csv_path: Path,
    components_csv_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for path, fields, rows in (
        (documents_csv_path, DOCUMENT_FIELDS, universe["documents"]),
        (components_csv_path, COMPONENT_FIELDS, universe["components"]),
    ):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        field: "; ".join(str(value) for value in row.get(field, []))
                        if isinstance(row.get(field), list)
                        else row.get(field)
                        for field in fields
                    }
                )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--remote-script", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--review-json", type=Path, required=True)
    parser.add_argument("--documents-csv", type=Path, required=True)
    parser.add_argument("--components-csv", type=Path, required=True)
    parser.add_argument("--baseline-capture", type=Path, required=True)
    parser.add_argument("--baseline-experiment-id", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--namespace", default="openrag")
    parser.add_argument("--deployment", default="openrag-backend")
    parser.add_argument("--timeout", type=int, default=1_800)
    args = parser.parse_args()

    spec = load_review_spec(args.spec)
    raw = capture_review_lanes(
        spec,
        script_path=args.remote_script,
        ssh_host=args.ssh_host,
        ssh_key=args.ssh_key,
        namespace=args.namespace,
        deployment=args.deployment,
        timeout=args.timeout,
    )
    args.raw_output.parent.mkdir(parents=True, exist_ok=True)
    args.raw_output.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    baseline = baseline_occurrences(_read_json(args.baseline_capture), args.baseline_experiment_id)
    universe = build_review_universe(
        spec,
        raw["captures"],
        baseline_occurrences=baseline,
    )
    universe["capture_reproducibility"] = {
        ("capture_spec_sha256" if key == "review_spec_sha256" else key): raw[key]
        for key in (
            "captured_at",
            "application_sha",
            "benchmark_tool_sha256",
            "remote_script_sha256",
            "review_spec_sha256",
            "evidence_context",
        )
    }
    write_review_artifacts(
        universe,
        json_path=args.review_json,
        documents_csv_path=args.documents_csv,
        components_csv_path=args.components_csv,
    )
    print(
        json.dumps(
            {
                "candidate_count": universe["candidate_count"],
                "component_count": universe["component_count"],
                "human_labels_filled": sum(
                    bool(row["human_label"]) for row in universe["documents"]
                ),
                "review_json": str(args.review_json),
            }
        )
    )


if __name__ == "__main__":
    main()
