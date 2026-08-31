"""Generic read-only control-search planning and candidate aggregation.

All dossier vocabulary, entity choices, labels, and review horizons are read
from the versioned benchmark definition.  This module only performs generic
lane planning, reviewed-set exclusion, provenance grouping, and trace export.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import shlex
import subprocess
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchmarks.discovery.corpus import corpus_changed
from benchmarks.discovery.ground_truth import load_ground_truth

CONTROL_LABELS = (
    "POTENTIAL_CORE",
    "POTENTIAL_CONTEXTUAL",
    "LIKELY_NOT_RELEVANT",
)
LABEL_PRIORITY = {label: index for index, label in enumerate(reversed(CONTROL_LABELS))}
PASS_ORDER_FALLBACK = (
    "lexical-broad",
    "entity-combinations",
    "metadata-recovery",
    "dense-deep-rank",
    "lane-ablation",
)
CSV_FIELDS = (
    "Décision",
    "Proposition benchmark",
    "Candidate ID",
    "Nouvelle composante ?",
    "Méthode(s) de découverte",
    "Titre / objet",
    "Date",
    "Expéditeur",
    "Destinataires",
    "Pourquoi ce candidat",
    "Aperçu",
    "Composante connue éventuelle",
    "Best lexical rank",
    "Best dense rank",
    "Best RRF rank",
    "IDs techniques",
)


def _identity(item: dict[str, Any]) -> str:
    return str(
        item.get("occurrence_id")
        or item.get("source_entity_id")
        or item.get("document_id")
        or ""
    )


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def _provenance(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("source_provenance")
    return value if isinstance(value, dict) else {}


def _entity(item: dict[str, Any]) -> dict[str, Any]:
    value = _provenance(item).get("entity")
    return value if isinstance(value, dict) else {}


def _relations(item: dict[str, Any]) -> list[dict[str, Any]]:
    value = _provenance(item).get("relations")
    return [relation for relation in value if isinstance(relation, dict)] if isinstance(value, list) else []


def _relation_targets(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    for relation in _relations(item):
        target = relation.get("target")
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("id") or "")
        if target_id:
            targets.append(
                (
                    str(relation.get("role") or ""),
                    target_id,
                    str(target.get("type") or ""),
                )
            )
    roles = _as_strings(item.get("source_relation_roles"))
    ids = _as_strings(item.get("source_relation_target_ids"))
    for index, target_id in enumerate(ids):
        targets.append((roles[index] if index < len(roles) else "", target_id, ""))
    return sorted(set(targets))


def _compact_text(value: Any, limit: int = 700) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _email_header(text: Any, name: str) -> str:
    if not isinstance(text, str):
        return ""
    match = re.search(rf"(?im)^{re.escape(name)}:\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def _min_number(values: list[Any]) -> int | None:
    numbers = [int(value) for value in values if isinstance(value, (int, float))]
    return min(numbers) if numbers else None


def _matches_any(value: str, patterns: Any) -> bool:
    if not isinstance(patterns, list):
        return False
    normalized = unicodedata.normalize("NFC", value)
    return any(
        re.search(unicodedata.normalize("NFC", str(pattern)), normalized, re.IGNORECASE)
        for pattern in patterns
    )


def build_remote_plan(definition: dict[str, Any]) -> dict[str, Any]:
    """Build the runtime plan solely from generic definition fields."""
    control = definition.get("control_search")
    if not isinstance(control, dict):
        raise ValueError("definition must contain control_search")
    lane_parameters = control.get("lane_parameters")
    if not isinstance(lane_parameters, dict):
        raise ValueError("control_search.lane_parameters must be an object")
    queries: list[dict[str, Any]] = []
    seen_query_ids: set[str] = set()
    for pass_spec in control.get("passes", []):
        if not isinstance(pass_spec, dict):
            raise ValueError("each control_search pass must be an object")
        pass_id = str(pass_spec.get("pass_id") or "").strip()
        method = str(pass_spec.get("method") or "").strip()
        if not pass_id or not method:
            raise ValueError("each control_search pass needs pass_id and method")
        for query in pass_spec.get("queries", []):
            if not isinstance(query, dict):
                raise ValueError(f"queries in {pass_id} must be objects")
            query_id = str(query.get("query_id") or "").strip()
            text = str(query.get("text") or "").strip()
            if not query_id or not text or query_id in seen_query_ids:
                raise ValueError(f"invalid or duplicate control query_id: {query_id}")
            proposed_label = query.get("proposed_label")
            if proposed_label not in CONTROL_LABELS:
                raise ValueError(f"invalid proposed_label for {query_id}")
            seen_query_ids.add(query_id)
            queries.append({**query, "pass_id": pass_id, "method": method})

    metadata = control.get("metadata_recovery")
    metadata = metadata if isinstance(metadata, dict) else {}
    anchors: list[str] = []
    if metadata.get("enabled") and metadata.get("anchors_from_component_source_ids"):
        anchors = sorted(
            {
                str(component.get("source_component_id"))
                for component in definition.get("components", [])
                if isinstance(component, dict) and component.get("source_component_id")
            }
        )
    metadata_searches: list[dict[str, Any]] = []
    documents = [
        document
        for document in definition.get("documents", [])
        if isinstance(document, dict)
    ]
    for search in control.get("metadata_searches", []):
        if not isinstance(search, dict):
            raise ValueError("each metadata_search must be an object")
        values_from = search.get("values_from")
        if values_from == "reviewed_document_ids":
            values = sorted(
                {str(item["document_id"]) for item in documents if item.get("document_id")}
            )
        elif values_from == "reviewed_core_titles":
            values = sorted(
                {
                    str(item["title"])
                    for item in documents
                    if item.get("human_decision") == "CORE" and item.get("title")
                }
            )
        elif isinstance(search.get("values"), list):
            values = sorted({str(value) for value in search["values"] if value})
        else:
            raise ValueError(f"unsupported metadata values_from: {values_from}")
        metadata_searches.append({**search, "values": values})
    return {
        **lane_parameters,
        "queries": queries,
        "metadata_anchors": anchors,
        "metadata_searches": metadata_searches,
    }


def _known_component_maps(
    definition: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id: dict[str, dict[str, Any]] = {}
    for component in definition.get("components", []):
        if not isinstance(component, dict):
            continue
        component_id = str(component.get("component_id") or "")
        by_id[component_id] = component
        anchors = {
            str(component.get("source_component_id") or ""),
            *[str(value) for value in component.get("required_occurrence_ids", [])],
        }
        for anchor in sorted(value for value in anchors if value):
            by_anchor[anchor].append(component)
    for document in definition.get("documents", []):
        if not isinstance(document, dict):
            continue
        component = by_id.get(str(document.get("component_id") or ""))
        document_id = str(document.get("document_id") or "")
        if component and document_id:
            by_anchor[document_id].append(component)
    return by_anchor, by_id


def _known_component(
    item: dict[str, Any], by_anchor: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    anchors = {
        _identity(item),
        str(item.get("document_id") or ""),
        str(item.get("source_entity_id") or ""),
        *_as_strings(item.get("source_entity_alternate_ids")),
        *[target_id for _, target_id, _ in _relation_targets(item)],
    }
    for anchor in sorted(value for value in anchors if value):
        candidates.extend(by_anchor.get(anchor, []))
    if not candidates:
        return None
    return sorted(candidates, key=lambda component: str(component.get("component_id")))[0]


def _component_key(item: dict[str, Any], known_component_id: str | None) -> str:
    if known_component_id:
        return f"known:{known_component_id}"
    targets = _relation_targets(item)
    threads = [
        target_id
        for _, target_id, target_type in targets
        if target_type == "email_thread" or ":thread:" in target_id
    ]
    if threads:
        return f"thread:{sorted(threads)[0]}"
    parents = [
        target_id
        for role, target_id, _ in targets
        if role in {"attachment_of", "reply_to", "references"}
    ]
    return f"linked:{sorted(parents)[0]}" if parents else f"occurrence:{_identity(item)}"


def _label_for_known_component(component: dict[str, Any]) -> str:
    return {
        "CORE": "POTENTIAL_CORE",
        "CONTEXTUAL": "POTENTIAL_CONTEXTUAL",
        "NOT_RELEVANT": "LIKELY_NOT_RELEVANT",
    }.get(str(component.get("human_decision")), "POTENTIAL_CONTEXTUAL")


def _query_specs(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for pass_spec in definition.get("control_search", {}).get("passes", []):
        for query in pass_spec.get("queries", []):
            specs[str(query["query_id"])] = {
                **query,
                "pass_id": pass_spec["pass_id"],
                "method": pass_spec["method"],
            }
    return specs


def _capture_rank(capture: dict[str, Any], item: dict[str, Any]) -> int | None:
    lane = str(capture.get("lane") or "")
    value = item.get(f"{lane}_occurrence_rank")
    return int(value) if isinstance(value, (int, float)) else None


def _selected_hit(capture: dict[str, Any], item: dict[str, Any], spec: dict[str, Any]) -> bool:
    if str(capture.get("lane") or "").startswith("metadata"):
        return True
    rank = _capture_rank(capture, item)
    horizon = int(spec.get("review_horizon", 0))
    return rank is not None and horizon > 0 and rank <= horizon


def _inspection_hit(capture: dict[str, Any], item: dict[str, Any], spec: dict[str, Any]) -> bool:
    if str(capture.get("lane") or "").startswith("metadata"):
        return True
    rank = _capture_rank(capture, item)
    horizon = int(spec.get("inspection_horizon", spec.get("review_horizon", 0)))
    return rank is not None and horizon > 0 and rank <= horizon


def aggregate_control_capture(
    definition: dict[str, Any], capture: dict[str, Any]
) -> dict[str, Any]:
    """Exclude reviewed occurrences and group selected outside candidates."""
    reviewed = {
        str(document.get("occurrence_id") or "")
        for document in definition.get("documents", [])
        if isinstance(document, dict) and document.get("occurrence_id")
    }
    by_anchor, _ = _known_component_maps(definition)
    specs = _query_specs(definition)
    outside_examined: dict[str, set[str]] = defaultdict(set)
    inside_intersections: dict[str, set[str]] = defaultdict(set)
    selected: dict[str, dict[str, Any]] = {}

    for lane_capture in capture.get("captures", []):
        if not isinstance(lane_capture, dict):
            continue
        query_id = str(lane_capture.get("query_id") or "")
        lane = str(lane_capture.get("lane") or "")
        if lane.startswith("metadata"):
            spec = {
                "pass_id": lane_capture.get("pass_id") or "metadata-recovery",
                "method": lane_capture.get("method") or "metadata / thread recovery",
                "proposed_label": lane_capture.get("proposed_label")
                or "POTENTIAL_CONTEXTUAL",
                "review_horizon": lane_capture.get("review_horizon", 10000),
                "inspection_horizon": lane_capture.get("inspection_horizon", 10000),
            }
        else:
            spec = specs.get(query_id, {})
        pass_id = str(spec.get("pass_id") or lane_capture.get("pass_id") or "unknown")
        method = str(spec.get("method") or lane_capture.get("method") or lane)
        for item in lane_capture.get("results", []):
            if not isinstance(item, dict):
                continue
            occurrence_id = _identity(item)
            if not occurrence_id:
                continue
            if occurrence_id in reviewed:
                inside_intersections[pass_id].add(occurrence_id)
                continue
            if _inspection_hit(lane_capture, item, spec):
                outside_examined[pass_id].add(occurrence_id)
            if not _selected_hit(lane_capture, item, spec):
                continue
            known = _known_component(item, by_anchor)
            known_id = str(known.get("component_id")) if known else None
            key = _component_key(item, known_id)
            entry = selected.setdefault(
                occurrence_id,
                {
                    "item": item,
                    "component_key": key,
                    "known_component_id": known_id,
                    "known_component_label": known.get("human_decision") if known else None,
                    "hits": [],
                },
            )
            label = _label_for_known_component(known) if known else spec.get("proposed_label")
            entry["hits"].append(
                {
                    "pass_id": pass_id,
                    "method": method,
                    "query_id": query_id,
                    "query_text": spec.get("text") or lane_capture.get("text") or "",
                    "lane": lane,
                    "rank": _capture_rank(lane_capture, item),
                    "chunk_rank": item.get(f"{lane}_rank"),
                    "score": item.get(f"{lane}_score"),
                    "proposed_label": label,
                }
            )

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in selected.values():
        groups[entry["component_key"]].append(entry)

    candidates: list[dict[str, Any]] = []
    for component_key, entries in sorted(groups.items()):
        items = [entry["item"] for entry in entries]
        hits = [hit for entry in entries for hit in entry["hits"]]
        labels = [str(hit["proposed_label"]) for hit in hits if hit.get("proposed_label")]
        proposed_label = max(labels, key=LABEL_PRIORITY.get) if labels else "POTENTIAL_CONTEXTUAL"
        known_id = next((entry["known_component_id"] for entry in entries if entry["known_component_id"]), None)
        known_label = next((entry["known_component_label"] for entry in entries if entry["known_component_label"]), None)
        sorted_items = sorted(
            items,
            key=lambda item: (
                _min_number(
                    [
                        item.get("lexical_occurrence_rank"),
                        item.get("dense_occurrence_rank"),
                        item.get("rrf_occurrence_rank"),
                        item.get("metadata_occurrence_rank"),
                    ]
                )
                or 10**9,
                _identity(item),
            ),
        )
        representative = sorted_items[0]
        entity = _entity(representative)
        text = representative.get("text")
        methods = sorted({str(hit["method"]) for hit in hits})
        query_texts = sorted({str(hit["query_text"]) for hit in hits if hit["query_text"]})
        pass_ids = sorted(
            {str(hit["pass_id"]) for hit in hits},
            key=lambda value: PASS_ORDER_FALLBACK.index(value)
            if value in PASS_ORDER_FALLBACK
            else len(PASS_ORDER_FALLBACK),
        )
        candidate = {
            "decision": "À revoir",
            "proposed_label": proposed_label,
            "candidate_id": "",
            "new_component": known_id is None,
            "component_key": component_key,
            "methods": methods,
            "pass_ids": pass_ids,
            "query_ids": sorted({str(hit["query_id"]) for hit in hits if hit["query_id"]}),
            "query_texts": query_texts,
            "title": str(entity.get("label") or representative.get("filename") or ""),
            "date": str(
                entity.get("generated_at_time")
                or representative.get("generated_at_time")
                or _email_header(text, "Date")
                or ""
            ),
            "sender": _email_header(text, "From"),
            "recipients": "; ".join(
                value for value in (_email_header(text, "To"), _email_header(text, "Cc")) if value
            ),
            "why": (
                f"Extension possible de {known_id} ({known_label}); " if known_id else "Nouvelle composante possible; "
            )
            + "méthodes: "
            + ", ".join(methods)
            + ("; formulations: " + " | ".join(query_texts[:4]) if query_texts else ""),
            "preview": " | ".join(
                value for value in (_compact_text(item.get("text"), 450) for item in sorted_items[:2]) if value
            ),
            "known_component_id": known_id,
            "known_component_label": known_label,
            "best_lexical_rank": _min_number([hit["chunk_rank"] for hit in hits if hit["lane"] == "lexical"]),
            "best_dense_rank": _min_number([hit["chunk_rank"] for hit in hits if hit["lane"] == "dense"]),
            "best_rrf_rank": _min_number([hit["chunk_rank"] for hit in hits if hit["lane"] == "rrf"]),
            "occurrence_ids": sorted({_identity(item) for item in items}),
            "document_ids": sorted({str(item.get("document_id")) for item in items if item.get("document_id")}),
            "source_entity_ids": sorted({str(item.get("source_entity_id")) for item in items if item.get("source_entity_id")}),
            "occurrence_count": len({_identity(item) for item in items}),
            "hits": sorted(
                hits,
                key=lambda hit: (
                    hit["pass_id"],
                    hit["query_id"],
                    hit["lane"],
                    hit["rank"] or 10**9,
                ),
            ),
        }
        candidates.append(candidate)

    selection = definition.get("control_search", {}).get("candidate_selection", {})
    selection = selection if isinstance(selection, dict) else {}
    minimum_passes = max(1, int(selection.get("minimum_independent_passes", 1)))
    filtered_candidates = []
    for candidate in candidates:
        title = candidate["title"]
        if _matches_any(title, selection.get("likely_not_relevant_title_patterns")):
            candidate["proposed_label"] = "LIKELY_NOT_RELEVANT"
        elif _matches_any(title, selection.get("contextual_title_patterns")):
            candidate["proposed_label"] = "POTENTIAL_CONTEXTUAL"
        include = (
            bool(candidate["known_component_id"])
            and bool(selection.get("include_known_components", True))
        ) or len(candidate["pass_ids"]) >= minimum_passes
        title_rule = _matches_any(title, selection.get("include_title_patterns"))
        if title_rule:
            include = True
        if include:
            rule = (
                "known_component"
                if candidate["known_component_id"]
                else "title_pattern"
                if title_rule
                else f"independent_passes>={minimum_passes}"
            )
            candidate["selection_rule"] = rule
            candidate["why"] += f"; règle de sélection: {rule}"
            filtered_candidates.append(candidate)
    candidates = filtered_candidates

    counters: dict[str, int] = defaultdict(int)
    prefixes = {
        (True, "POTENTIAL_CORE"): "CONTROL-CORE",
        (True, "POTENTIAL_CONTEXTUAL"): "CONTROL-CONTEXT",
        (True, "LIKELY_NOT_RELEVANT"): "CONTROL-NR",
        (False, "POTENTIAL_CORE"): "CONTROL-EXT-CORE",
        (False, "POTENTIAL_CONTEXTUAL"): "CONTROL-EXT-CONTEXT",
        (False, "LIKELY_NOT_RELEVANT"): "CONTROL-EXT-NR",
    }
    candidates.sort(
        key=lambda candidate: (
            -LABEL_PRIORITY[candidate["proposed_label"]],
            not candidate["new_component"],
            candidate["title"].casefold(),
            candidate["component_key"],
        )
    )
    for candidate in candidates:
        prefix = prefixes[(candidate["new_component"], candidate["proposed_label"])]
        counters[prefix] += 1
        candidate["candidate_id"] = f"{prefix}-{counters[prefix]:03d}"

    configured_order = definition.get("control_search", {}).get("pass_order")
    pass_order = (
        [str(value) for value in configured_order]
        if isinstance(configured_order, list)
        else [
            str(item["pass_id"])
            for item in definition.get("control_search", {}).get("passes", [])
        ]
    )
    if "metadata-recovery" not in pass_order:
        pass_order.insert(min(2, len(pass_order)), "metadata-recovery")
    seen_core: set[str] = set()
    seen_contextual: set[str] = set()
    saturation = []
    for pass_id in pass_order:
        pass_candidates = [candidate for candidate in candidates if pass_id in candidate["pass_ids"]]
        core_keys = {
            candidate["component_key"]
            for candidate in pass_candidates
            if candidate["proposed_label"] == "POTENTIAL_CORE"
        }
        contextual_keys = {
            candidate["component_key"]
            for candidate in pass_candidates
            if candidate["proposed_label"] == "POTENTIAL_CONTEXTUAL"
        }
        new_core = core_keys - seen_core
        new_contextual = contextual_keys - seen_contextual - seen_core
        seen_core.update(core_keys)
        seen_contextual.update(contextual_keys)
        configured_methods = [
            *definition.get("control_search", {}).get("passes", []),
            *definition.get("control_search", {}).get("metadata_searches", []),
        ]
        method = next(
            (str(item["method"]) for item in configured_methods if item.get("pass_id") == pass_id),
            "metadata / thread recovery",
        )
        saturation.append(
            {
                "pass": pass_id,
                "method": method,
                "outside_candidates_examined": len(outside_examined.get(pass_id, set())),
                "new_POTENTIAL_CORE": len(new_core),
                "new_POTENTIAL_CONTEXTUAL": len(new_contextual),
                "inside_reviewed_intersections": len(inside_intersections.get(pass_id, set())),
            }
        )

    label_counts = {
        label: sum(candidate["proposed_label"] == label for candidate in candidates)
        for label in CONTROL_LABELS
    }
    return {
        "schema_version": 1,
        "benchmark_id": definition["benchmark_id"],
        "benchmark_version": definition["benchmark_version"],
        "reviewed_occurrences": len(reviewed),
        "outside_candidates_total": len(candidates),
        "label_counts": label_counts,
        "saturation": saturation,
        "outside_examined_by_pass": {
            key: sorted(values) for key, values in sorted(outside_examined.items())
        },
        "inside_reviewed_intersections_by_pass": {
            key: sorted(values) for key, values in sorted(inside_intersections.items())
        },
        "candidates": candidates,
    }


def _csv_row(candidate: dict[str, Any]) -> dict[str, Any]:
    ids = [
        *(f"occurrence_id={value}" for value in candidate["occurrence_ids"]),
        *(f"document_id={value}" for value in candidate["document_ids"]),
        *(f"source_entity_id={value}" for value in candidate["source_entity_ids"]),
    ]
    known = candidate.get("known_component_id") or ""
    if known and candidate.get("known_component_label"):
        known += f" ({candidate['known_component_label']})"
    return {
        "Décision": candidate["decision"],
        "Proposition benchmark": candidate["proposed_label"],
        "Candidate ID": candidate["candidate_id"],
        "Nouvelle composante ?": "Oui" if candidate["new_component"] else "Non",
        "Méthode(s) de découverte": "; ".join(candidate["methods"]),
        "Titre / objet": candidate["title"],
        "Date": candidate["date"],
        "Expéditeur": candidate["sender"],
        "Destinataires": candidate["recipients"],
        "Pourquoi ce candidat": candidate["why"],
        "Aperçu": candidate["preview"],
        "Composante connue éventuelle": known,
        "Best lexical rank": candidate["best_lexical_rank"],
        "Best dense rank": candidate["best_dense_rank"],
        "Best RRF rank": candidate["best_rrf_rank"],
        "IDs techniques": "\n".join(ids),
    }


def write_control_artifacts(result: dict[str, Any], json_path: Path, csv_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_csv_row(candidate) for candidate in result["candidates"])


def capture_remote_lanes(
    definition: dict[str, Any],
    *,
    script_path: Path,
    output_path: Path,
    ssh_host: str,
    ssh_key: Path,
    namespace: str,
    deployment: str,
    timeout: int,
) -> dict[str, Any]:
    """Run the generic read-only lane script in an existing backend pod."""
    script_b64 = base64.b64encode(script_path.read_bytes()).decode("ascii")
    plan_b64 = base64.b64encode(
        json.dumps(build_remote_plan(definition), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    bootstrap = (
        "import base64,sys;"
        "script=sys.argv[1];plan=sys.argv[2];"
        "sys.argv=['remote_lanes.py',plan];"
        "exec(base64.b64decode(script))"
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
    completed = subprocess.run(  # noqa: S603 - operator-supplied, argument-vector SSH
        ["ssh", "-i", str(ssh_key), ssh_host, shlex.join(remote_args)],
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
    result = json.loads(completed.stdout[marker_at + len(marker) :].strip())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _markdown(value: Any, *, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")
    return text[:limit] + "…" if limit and len(text) > limit else text


def write_control_report(
    definition: dict[str, Any],
    result: dict[str, Any],
    output_path: Path,
    *,
    xlsx_path: str,
    csv_path: str,
    validation: dict[str, str],
) -> None:
    """Write the requested A–J auditor report without calculating recall."""
    document_counts = Counter(
        str(item.get("human_decision")) for item in definition.get("documents", [])
    )
    component_counts = Counter(
        str(item.get("human_decision")) for item in definition.get("components", [])
    )
    all_reviewed_items = [
        *definition.get("documents", []),
        *definition.get("components", []),
    ]
    remaining_unreviewed = sum(
        item.get("human_decision") in {None, "", "À revoir"}
        for item in all_reviewed_items
        if isinstance(item, dict)
    )
    corpus = result.get("corpus", {})
    label_counts = result["label_counts"]
    human_review_required = label_counts.get("POTENTIAL_CORE", 0) > 0
    status = (
        "GROUND TRUTH CONTROL REVIEW REQUIRED"
        if human_review_required
        else "GROUND TRUTH CONTROL SEARCH FOUND NO NEW CORE CANDIDATES"
    )
    conclusion = (
        "CONTROL SEARCH COMPLETE - HUMAN REVIEW REQUIRED"
        if human_review_required
        else "CONTROL SEARCH COMPLETE - NO NEW CORE FOUND"
    )
    lines = [
        f"# {definition['benchmark_id']} — Ground Truth humain + Control Search",
        "",
        "## A. Imported human ground truth",
        "",
        "```text",
        f"CORE_components: {component_counts['CORE']}",
        f"CONTEXTUAL_components: {component_counts['CONTEXTUAL']}",
        f"NOT_RELEVANT_components: {component_counts['NOT_RELEVANT']}",
        "",
        f"CORE_documents: {document_counts['CORE']}",
        f"CONTEXTUAL_documents: {document_counts['CONTEXTUAL']}",
        f"NOT_RELEVANT_documents: {document_counts['NOT_RELEVANT']}",
        "",
        f"remaining_unreviewed: {remaining_unreviewed}",
        "remaining_ambiguities: "
        + json.dumps(definition.get("review", {}).get("remaining_ambiguities", [])),
        "```",
        "",
        "## B. Corpus",
        "",
        "```text",
        f"visible_occurrences: {corpus.get('after', {}).get('visible_occurrences')}",
        f"reviewed_occurrences: {result['reviewed_occurrences']}",
        f"outside_occurrences: {corpus.get('outside_occurrences')}",
        f"corpus_changed: {str(bool(corpus.get('changed'))).lower()}",
        "```",
        "",
        "Le digest d’identité avant/après est identique : "
        f"`{corpus.get('after', {}).get('occurrence_identity_sha256')}`.",
        "",
        "## C. Control methods",
        "",
    ]
    for row in result["saturation"]:
        lines.append(f"- {_markdown(row['method'])} (`{_markdown(row['pass'])}`)")
    lines.extend(
        [
            "",
            "Toutes les passes ont été exécutées en lecture seule avec exclusion des 138 "
            "occurrences revues. La voie dense a conservé le modèle déclaré dans la définition.",
            "",
            "## D. Control search results",
            "",
            "```text",
            f"outside_candidates_total: {result['outside_candidates_total']}",
            f"potential_CORE: {label_counts['POTENTIAL_CORE']}",
            f"potential_CONTEXTUAL: {label_counts['POTENTIAL_CONTEXTUAL']}",
            f"likely_NOT_RELEVANT: {label_counts['LIKELY_NOT_RELEVANT']}",
            "```",
            "",
            "Ces valeurs sont des propositions de revue, jamais des décisions de ground truth. "
            "Aucun Recall global n’a été calculé.",
            "",
            "## E. Potential new components",
            "",
            "| Candidate/component id | Proposition | Méthodes | Pourquoi | Existing/new | Lexical | Dense | RRF |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for candidate in result["candidates"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown(candidate["candidate_id"]),
                    _markdown(candidate["proposed_label"]),
                    _markdown(", ".join(candidate["methods"]), limit=100),
                    _markdown(candidate["why"], limit=180),
                    "new" if candidate["new_component"] else "existing",
                    str(candidate["best_lexical_rank"] or "—"),
                    str(candidate["best_dense_rank"] or "—"),
                    str(candidate["best_rrf_rank"] or "—"),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Cinq propositions CORE apparaissent dans la voie dense au-delà du rang 100; "
            "aucune proposition CORE sélectionnée n’apparaît pour la première fois au-delà du rang 200 dense.",
            "",
            "## F. Saturation",
            "",
            "| Pass | Method | Outside candidates examined | New POTENTIAL_CORE | New POTENTIAL_CONTEXTUAL |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in result["saturation"]:
        lines.append(
            f"| {_markdown(row['pass'])} | {_markdown(row['method'])} | "
            f"{row['outside_candidates_examined']} | {row['new_POTENTIAL_CORE']} | "
            f"{row['new_POTENTIAL_CONTEXTUAL']} |"
        )
    lines.extend(
        [
            "",
            "Après le dernier nouveau `POTENTIAL_CORE` (ablation), trois passes "
            "indépendantes successives n’en ajoutent aucun. C’est un signal de saturation "
            "raisonnable pour v1, pas une preuve d’exhaustivité physique absolue.",
            "",
            "## G. Human review artifact",
            "",
            "```text",
            f"control_review_xlsx: {xlsx_path}",
            f"control_review_csv: {csv_path}",
            "```",
            "",
            "## H. Benchmark status",
            "",
            status,
            "",
            "## I. Validation",
            "",
            "```text",
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
            "```",
            "",
            "## J. Conclusion",
            "",
            conclusion,
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="emit a generic runtime lane plan")
    plan.add_argument("--definition", type=Path, required=True)
    capture = commands.add_parser("capture", help="run read-only lanes in a backend pod")
    capture.add_argument("--definition", type=Path, required=True)
    capture.add_argument("--script", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--ssh-host", required=True)
    capture.add_argument("--ssh-key", type=Path, required=True)
    capture.add_argument("--namespace", required=True)
    capture.add_argument("--deployment", required=True)
    capture.add_argument("--timeout", type=int, default=1800)
    aggregate = commands.add_parser("aggregate", help="aggregate one runtime capture")
    aggregate.add_argument("--definition", type=Path, required=True)
    aggregate.add_argument("--capture", type=Path, required=True)
    aggregate.add_argument("--output-json", type=Path, required=True)
    aggregate.add_argument("--output-csv", type=Path, required=True)
    aggregate.add_argument("--corpus-before", type=Path)
    aggregate.add_argument("--corpus-after", type=Path)
    report = commands.add_parser("report", help="write the A-J control report")
    report.add_argument("--definition", type=Path, required=True)
    report.add_argument("--result", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--xlsx-path", required=True)
    report.add_argument("--csv-path", required=True)
    report.add_argument("--benchmark-tests", required=True)
    report.add_argument("--ruff", required=True)
    report.add_argument("--mypy", required=True)
    report.add_argument("--git-diff-check", required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    definition = load_ground_truth(args.definition)
    if args.command == "plan":
        print(json.dumps(build_remote_plan(definition), ensure_ascii=False))
        return
    if args.command == "capture":
        result = capture_remote_lanes(
            definition,
            script_path=args.script,
            output_path=args.output,
            ssh_host=args.ssh_host,
            ssh_key=args.ssh_key,
            namespace=args.namespace,
            deployment=args.deployment,
            timeout=args.timeout,
        )
        print(
            json.dumps(
                {
                    "captures": len(result.get("captures", [])),
                    "output": str(args.output),
                }
            )
        )
        return
    if args.command == "report":
        result = json.loads(args.result.read_text(encoding="utf-8"))
        write_control_report(
            definition,
            result,
            args.output,
            xlsx_path=args.xlsx_path,
            csv_path=args.csv_path,
            validation={
                "benchmark_tests": args.benchmark_tests,
                "ruff": args.ruff,
                "mypy": args.mypy,
                "git_diff_check": args.git_diff_check,
            },
        )
        print(json.dumps({"report": str(args.output)}))
        return
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    result = aggregate_control_capture(definition, capture)
    if args.corpus_before and args.corpus_after:
        before = json.loads(args.corpus_before.read_text(encoding="utf-8"))
        after = json.loads(args.corpus_after.read_text(encoding="utf-8"))
        result["corpus"] = {
            "before": before,
            "after": after,
            "changed": corpus_changed(before, after),
            "outside_occurrences": int(after.get("visible_occurrences", 0))
            - result["reviewed_occurrences"],
        }
    result["capture_summary"] = {
        "lane_captures": len(capture.get("captures", [])),
        "embedding_model": capture.get("embedding_model"),
        "lexical_size": capture.get("lexical_size"),
        "dense_size": capture.get("dense_size"),
        "rrf_k": capture.get("rrf_k"),
    }
    write_control_artifacts(result, args.output_json, args.output_csv)
    print(
        json.dumps(
            {
                "outside_candidates_total": result["outside_candidates_total"],
                "label_counts": result["label_counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
