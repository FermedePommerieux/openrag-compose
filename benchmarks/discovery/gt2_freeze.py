"""Freeze the reviewed Orange/Fibre GT2 without assigning automatic qrels.

The pass-3 import is deliberately a tiny JSON artifact containing only the two
human-owned columns.  Candidate identity and documentary metadata are joined
from the previously versioned, unlabeled completeness artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

from benchmarks.discovery.cross_domain_review import build_review_universe, load_review_spec
from benchmarks.discovery.gt2_consolidation import (
    QREL_MAPPING,
    canonical_sha256,
    consolidate_document_rows,
    freeze_gate,
    generate_title_family_candidates,
    validate_human_rows,
)

TOPIC_VERSION = "orange-fibre-cross-domain-v1"
GUIDELINE_VERSION = "orange-fibre-cross-domain-guideline-v1"
CORPUS_DIGEST = "038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7"
PASS3_WORKBOOK = "orange-fibre-GT2-completeness-review-pass-3.xlsx"
PASS3_SHA256 = "9745b82639775948aa0a4efcb3ae92f3338a244f1885b898bb1006180cb93fb5"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _with_artifact_sha256(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["artifact_sha256"] = canonical_sha256(result)
    return result


def _fold(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )


def _candidate_text(row: dict[str, Any]) -> str:
    return _fold(" ".join(str(row.get(key) or "") for key in ("title", "text_preview")))


def _sender_domain(value: str) -> str:
    match = re.search(r"@([A-Za-z0-9.-]+)", value)
    return match.group(1).lower().rstrip(".") if match else ""


def _long_numeric_ids(row: dict[str, Any]) -> set[str]:
    return set(re.findall(r"(?<!\d)\d{6,}(?!\d)", _candidate_text(row)))


def _reviewed_workbook_rows(path: Path) -> list[dict[str, Any]]:
    """Read the human-owned pass-3 cells from the digest-pinned XLSX archive."""

    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns = {"m": namespace}
    with ZipFile(path) as archive:
        shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(node.text or "" for node in item.iter(f"{{{namespace}}}t"))
            for item in shared_root.findall("m:si", ns)
        ]
        sheet = ElementTree.fromstring(
            archive.read("xl/worksheets/sheet3.xml")
        )

    rows: list[dict[str, Any]] = []
    for row in sheet.findall(".//m:sheetData/m:row", ns)[1:]:
        values: dict[str, str] = {}
        for cell in row.findall("m:c", ns):
            column = re.match(r"[A-Z]+", str(cell.get("r")))
            if column is None:
                continue
            value_node = cell.find("m:v", ns)
            value = value_node.text or "" if value_node is not None else ""
            if cell.get("t") == "s" and value:
                value = shared[int(value)]
            values[column.group()] = value
        rows.append(
            {
                "candidate_id": values.get("W", ""),
                "human_label": values.get("D", ""),
                "review_notes": values.get("E", ""),
                "review_row": int(str(row.get("r"))),
            }
        )
    return rows


def _pass3_qrels(
    pass3_import: dict[str, Any], prior_candidates: dict[str, Any], raw_workbook: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if _sha256_file(raw_workbook) != PASS3_SHA256:
        raise ValueError("pass-3 workbook SHA-256 does not match the reviewed source")
    if pass3_import.get("imported_columns") != ["human_label", "review_notes"]:
        raise ValueError("pass-3 import must contain only the two human-owned columns")
    if pass3_import.get("source_workbook_sha256") != PASS3_SHA256:
        raise ValueError("pass-3 import provenance digest mismatch")

    expected = {str(row["candidate_id"]): row for row in prior_candidates["candidates"]}
    imported = list(pass3_import["rows"])
    audit = validate_human_rows(imported, identity_field="candidate_id")
    if imported != _reviewed_workbook_rows(raw_workbook):
        raise ValueError("pass-3 import differs from the human-owned workbook cells")
    imported_ids = [str(row["candidate_id"]) for row in imported]
    if set(imported_ids) != set(expected) or len(imported_ids) != len(expected):
        raise ValueError("pass-3 candidate identities differ from the selected review tranche")
    if not audit["valid"]:
        raise ValueError(f"pass-3 human fields are invalid: {audit}")

    rows = []
    for human in imported:
        candidate_id = str(human["candidate_id"])
        source = expected[candidate_id]
        rows.append(
            {
                "case_id": TOPIC_VERSION,
                "candidate_id": candidate_id,
                "component_id": source["component_id"],
                "document_id": source["document_id"],
                "occurrence_id": source["occurrence_id"],
                "source_entity_id": source["source_entity_id"],
                "title": source.get("title", ""),
                "human_label": human["human_label"],
                "review_notes": human.get("review_notes", ""),
                "qrel_grade": QREL_MAPPING[human["human_label"]],
                "review_source_file": PASS3_WORKBOOK,
                "review_sheet": pass3_import["review_sheet"],
                "review_row": human["review_row"],
                "review_stage": pass3_import["review_stage"],
                "topic_version": TOPIC_VERSION,
                "guideline_version": GUIDELINE_VERSION,
            }
        )
    rows.sort(key=lambda row: str(row["candidate_id"]))
    return rows, {**audit, "workbook_values_match": True}


def _full_universe(root: Path) -> dict[str, Any]:
    spec = load_review_spec(
        root / "benchmarks/discovery/review_specs/orange-fibre-cross-domain-v1.yaml"
    )
    raw = _load(
        root / "benchmarks/discovery/results/orange-fibre-cross-domain-v1-unlabeled-lanes.json"
    )
    unbounded = {
        **spec,
        "review_selection": {
            "max_components": 100_000,
            "minimum_independent_queries": 1,
            "max_control_components": 100_000,
            "include_metadata_only": True,
        },
    }
    return build_review_universe(
        unbounded,
        raw["captures"],
        baseline_occurrences=set(),
    )


def _completeness_control(
    universe: dict[str, Any], judged: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_id = {str(row["candidate_id"]): row for row in universe["documents"]}
    judged_ids = {str(row["candidate_id"]) for row in judged}
    unjudged = [row for row in universe["documents"] if row["candidate_id"] not in judged_ids]
    enriched = [{**by_id.get(str(row["candidate_id"]), {}), **row} for row in judged]
    core = [row for row in enriched if row["human_label"] == "CORE"]
    relevant = [row for row in enriched if row["human_label"] in {"CORE", "CONTEXTUAL"}]

    core_components = {str(row["component_id"]) for row in core}
    relevant_components = {str(row["component_id"]) for row in relevant}
    core_document_ids = {str(row["document_id"]) for row in core if row.get("document_id")}
    relevant_document_ids = {
        str(row["document_id"]) for row in relevant if row.get("document_id")
    }
    core_domains = {_sender_domain(str(row.get("sender") or "")) for row in core}
    core_domains.discard("")
    core_numeric_ids = set().union(*(_long_numeric_ids(row) for row in core))
    relevant_numeric_ids = set().union(*(_long_numeric_ids(row) for row in relevant))

    orange = re.compile(r"\b(?:orange|sosh)\b", re.I)
    fixed = re.compile(
        r"\b(?:fibre|fiber|ftth|livebox|internet\s+fixe|acces\s+internet)\b",
        re.I,
    )
    fixed_event = re.compile(r"\b(?:raccordement|installation|resiliation|incident)\b", re.I)
    secondary = re.compile(
        r"\b(?:sig|comptabilite|comptable|tableau|rapport|presentation|plaquette|budget|transversal)\b",
        re.I,
    )
    mobile = re.compile(r"\b(?:mobile|4g|5g|telephone|telephonie|smartphone)\b", re.I)
    adsl = re.compile(r"\b(?:adsl|dsl)\b", re.I)

    def select(predicate: Any) -> list[dict[str, Any]]:
        return sorted(
            [row for row in unjudged if predicate(row)],
            key=lambda row: str(row["candidate_id"]),
        )

    same_core_component = select(lambda row: row["component_id"] in core_components)
    same_relevant_component = select(lambda row: row["component_id"] in relevant_components)
    same_core_document = select(lambda row: row.get("document_id") in core_document_ids)
    same_relevant_document = select(lambda row: row.get("document_id") in relevant_document_ids)
    title_family = generate_title_family_candidates(universe["documents"], judged_rows=judged)
    direct = select(
        lambda row: bool(orange.search(_candidate_text(row)) and fixed.search(_candidate_text(row)))
    )
    secondary_context = select(
        lambda row: bool(
            orange.search(_candidate_text(row))
            and fixed.search(_candidate_text(row))
            and secondary.search(_candidate_text(row))
        )
    )

    selected_by_id: dict[str, dict[str, Any]] = {}
    for selection_class, rows in (
        ("SAME_HUMAN_CORE_COMPONENT", same_core_component),
        ("SAME_RELEVANT_COMPONENT", same_relevant_component),
        ("SAME_HUMAN_CORE_DOCUMENT_ID", same_core_document),
        ("SAME_RELEVANT_DOCUMENT_ID", same_relevant_document),
        ("DIRECT_ORANGE_SOSH_PLUS_FIXED_SERVICE", direct),
        ("SECONDARY_CONTEXT_ORANGE_SOSH_PLUS_FIXED_SERVICE", secondary_context),
    ):
        for row in rows:
            selected_by_id.setdefault(
                str(row["candidate_id"]),
                {**row, "selection_classes": []},
            )["selection_classes"].append(selection_class)
    for row in title_family:
        selected_by_id.setdefault(
            str(row["candidate_id"]),
            {**row, "selection_classes": []},
        )["selection_classes"].append(str(row["selection_class"]))
    pending = sorted(selected_by_id.values(), key=lambda row: str(row["candidate_id"]))

    sender_domain = select(
        lambda row: _sender_domain(str(row.get("sender") or "")) in core_domains
    )
    sender_domain_plus_fixed = select(
        lambda row: _sender_domain(str(row.get("sender") or "")) in core_domains
        and bool(fixed.search(_candidate_text(row)))
    )
    shared_core_identifier = select(
        lambda row: bool(_long_numeric_ids(row) & core_numeric_ids)
    )
    shared_relevant_identifier = select(
        lambda row: bool(_long_numeric_ids(row) & relevant_numeric_ids)
    )
    orange_event_without_service = select(
        lambda row: bool(
            orange.search(_candidate_text(row))
            and fixed_event.search(_candidate_text(row))
            and not fixed.search(_candidate_text(row))
        )
    )
    fixed_without_brand = select(
        lambda row: bool(fixed.search(_candidate_text(row)) and not orange.search(_candidate_text(row)))
    )
    mobile_only = select(
        lambda row: bool(mobile.search(_candidate_text(row)) and not fixed.search(_candidate_text(row)))
    )
    adsl_only = select(
        lambda row: bool(adsl.search(_candidate_text(row)) and not fixed.search(_candidate_text(row)))
    )

    diagnostics = {
        "same_thread_or_component_as_human_core": len(same_core_component),
        "same_thread_or_component_as_any_relevant": len(same_relevant_component),
        "same_document_id_as_human_core": len(same_core_document),
        "same_document_id_as_any_relevant": len(same_relevant_document),
        "exact_or_near_human_core_title_family": len(title_family),
        "direct_orange_sosh_plus_fixed_service": len(direct),
        "secondary_context_orange_sosh_plus_fixed_service": len(secondary_context),
        "same_sender_domain_diagnostic_only": len(sender_domain),
        "same_sender_domain_plus_fixed_without_brand_diagnostic_only": len(
            sender_domain_plus_fixed
        ),
        "shared_human_core_numeric_identifier_diagnostic_only": len(
            shared_core_identifier
        ),
        "shared_any_relevant_numeric_identifier_diagnostic_only": len(
            shared_relevant_identifier
        ),
        "orange_word_plus_event_without_fixed_service_diagnostic_only": len(
            orange_event_without_service
        ),
        "fixed_service_without_orange_sosh_diagnostic_only": len(fixed_without_brand),
        "mobile_only_false_positive_guard": len(mobile_only),
        "adsl_dsl_only_false_positive_guard": len(adsl_only),
        "deep_lexical_dense_and_metadata_evidence_already_in_universe": True,
        "title_similarity_threshold": 0.82,
    }
    diagnostic_examples = {
        "same_sender_domain_plus_fixed_without_brand": [
            row["candidate_id"] for row in sender_domain_plus_fixed[:10]
        ],
        "orange_word_plus_event_without_fixed_service": [
            row["candidate_id"] for row in orange_event_without_service[:10]
        ],
        "fixed_service_without_orange_sosh": [
            row["candidate_id"] for row in fixed_without_brand[:10]
        ],
        "mobile_only_guard": [row["candidate_id"] for row in mobile_only[:10]],
        "adsl_dsl_only_guard": [row["candidate_id"] for row in adsl_only[:10]],
    }
    return (
        {
            "schema_version": 1,
            "artifact_type": "gt2_completeness_control_final",
            "case_id": TOPIC_VERSION,
            "corpus_digest": CORPUS_DIGEST,
            "pooling_method": (
                "TREC-style judged pool plus deep lexical/dense lanes, metadata/relation "
                "recovery, relevant-neighbor probes, title families, and the acquired "
                "outside-priority negative control."
            ),
            "candidate_universe": {
                "documents": len(universe["documents"]),
                "components": len(universe["components"]),
                "judged_documents": len(judged_ids),
                "unjudged_documents": len(unjudged),
            },
            "probe_diagnostics": diagnostics,
            "diagnostic_examples": diagnostic_examples,
            "high_priority_candidates_found": len(pending),
            "human_review_needed": len(pending),
            "candidates": pending,
            "auto_labels_created": 0,
            "unjudged_policy": (
                "Unjudged documents remain UNJUDGED, are excluded from qrels and judged-only "
                "precision, and are never defaulted to NOT_RELEVANT."
            ),
            "completeness_control_completed": True,
        },
        pending,
    )


def _metric_components(
    qrels: list[dict[str, Any]], component_judgments: list[dict[str, Any]], universe: dict[str, Any]
) -> list[dict[str, Any]]:
    human_component = {
        str(row["component_id"]): str(row["human_label"]) for row in component_judgments
    }
    universe_documents = {
        str(row["candidate_id"]): row for row in universe["documents"]
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in qrels:
        grouped[str(row["component_id"])].append(row)
    result = []
    for component_id, rows in sorted(grouped.items()):
        labels = Counter(str(row["human_label"]) for row in rows)
        decision = max(labels, key=lambda label: QREL_MAPPING[label])
        sample = universe_documents.get(str(rows[0]["candidate_id"]), {})
        component_key = str(sample.get("component_key") or "")
        component_type = (
            "email_thread"
            if component_key.startswith("thread:")
            else "explicit_document_group"
            if component_key.startswith("linked:")
            else "standalone_document"
        )
        result.append(
            {
                "component_id": component_id,
                "state": {
                    "CORE": "relevant",
                    "CONTEXTUAL": "uncertain",
                    "NOT_RELEVANT": "not_relevant",
                }[decision],
                "human_decision": decision,
                "type": component_type,
                "required_occurrence_ids": sorted(str(row["occurrence_id"]) for row in rows),
                "document_qrel_label_counts": dict(sorted(labels.items())),
                "reviewed_component_human_decision": human_component.get(component_id),
                "component_metric_semantics": (
                    "Derived only from human document qrels by maximum grade; the separately "
                    "reviewed component label is metadata and was not propagated to documents."
                ),
            }
        )
    return result


def _ground_truth(
    qrels: list[dict[str, Any]],
    source: dict[str, Any],
    completeness: dict[str, Any],
    universe: dict[str, Any],
) -> dict[str, Any]:
    documents = []
    for row in qrels:
        documents.append(
            {
                "occurrence_id": row["occurrence_id"],
                "document_id": row["document_id"],
                "source_entity_id": row["source_entity_id"],
                "component_id": row["component_id"],
                "candidate_id": row["candidate_id"],
                "state": {
                    "CORE": "relevant",
                    "CONTEXTUAL": "uncertain",
                    "NOT_RELEVANT": "not_relevant",
                }[row["human_label"]],
                "human_decision": row["human_label"],
                "qrel_grade": row["qrel_grade"],
                "title": row.get("title", ""),
                "review_notes": row.get("review_notes", ""),
                "review_source_file": row["review_source_file"],
                "review_stage": row["review_stage"],
                "review_sheet": row["review_sheet"],
                "review_row": row["review_row"],
                "topic_version": row["topic_version"],
                "guideline_version": row["guideline_version"],
            }
        )
    components = _metric_components(qrels, source["component_judgments"], universe)
    document_counts = Counter(row["human_label"] for row in qrels)
    component_counts = Counter(row["human_decision"] for row in components)
    value: dict[str, Any] = {
        "schema_version": 1,
        "benchmark_id": TOPIC_VERSION,
        "benchmark_version": 1,
        "benchmark_case_number": 2,
        "document_metric_unit": "source_occurrence",
        "topic": {
            "name": "Orange fibre cross-domain",
            "definition": (
                "Orange/Sosh AND fibre or fixed Internet AND thematic centrality for CORE; "
                "useful but secondary Orange plus fixed-Internet information for CONTEXTUAL; "
                "otherwise NOT_RELEVANT."
            ),
            "guideline_version": GUIDELINE_VERSION,
            "human_substitution_test": {
                "brand": (
                    "Replace Orange/Sosh with Free, SFR, or Bouygues; if relevance is unchanged, "
                    "the document is not CORE."
                ),
                "service": (
                    "Replace fibre/fixed Internet with mobile, telephony, or ADSL; if relevance "
                    "is unchanged, the document is not CORE."
                ),
                "automation_policy": "Human guideline only; never an automatic retrieval rule.",
            },
        },
        "queries": [
            {
                "query_id": "canonical-literal",
                "kind": "canonical_literal",
                "text": "Tous les échanges avec Orange au sujet de la fibre.",
            }
        ],
        "relevance_views": {"STRICT": ["CORE"], "BROAD": ["CORE", "CONTEXTUAL"]},
        "qrel_mapping": QREL_MAPPING,
        "baseline_run": {
            "source_sha": "477092776baaacfc9fb6131766e83b32f60b181d",
            "tag": "v0.6.0-retrieval-v2-prov-o-scope-policy-v1",
            "scope_policy_id": "documentary-prov-o",
            "scope_policy_version": 1,
            "k_values": [100],
            "retrieval": {
                "strategy": "rrf",
                "mode": "hybrid",
                "lexical_candidates": 50,
                "vector_candidates": 50,
                "rrf_k": 60,
                "max_chunks_per_document": 3,
                "adaptive_max_chunks_per_document": 20,
                "reranker_enabled": False,
            },
            "embedding": {"provider": "openai", "model": "text-embedding-3-large"},
            "chunking": {
                "strategy": "hybrid",
                "hybrid_max_tokens": 512,
                "hybrid_merge_peers": True,
            },
        },
        "historical_runtime_references": {
            "scope_policy_v1": {
                "tag": "v0.6.0-retrieval-v2-prov-o-scope-policy-v1",
                "commit_sha": "477092776baaacfc9fb6131766e83b32f60b181d",
            },
            "measured_runtime_reference": "scope_policy_v1",
        },
        "human_validation_pipeline": {
            "component_judgments": len(source["component_judgments"]),
            "document_review_stages": [
                {
                    "stage": stage["stage"],
                    "documents": len(stage["rows"]),
                    "label_counts": dict(
                        sorted(Counter(row["human_label"] for row in stage["rows"]).items())
                    ),
                }
                for stage in source["document_review_stages"]
            ],
            "total_unique_human_document_judgments": len(qrels),
            "remaining_selected_review_items": completeness["human_review_needed"],
            "unjudged_candidate_universe": completeness["candidate_universe"][
                "unjudged_documents"
            ],
            "human_review_complete_for_selected_pool": True,
            "mixed_component_document_decisions_preserved": True,
            "component_labels_propagated_to_documents": False,
        },
        "ground_truth_provenance": {
            "source_type": "human_review",
            "source_workbooks": source["source_workbooks"],
            "human_review_source_export_sha256": source["source_export_sha256"],
            "completeness_control_artifact_sha256": completeness["artifact_sha256"],
            "corpus_digest": CORPUS_DIGEST,
            "freeze_date": "2026-09-01",
            "freeze_status": "FROZEN",
            "human_judgment_precedence": source["human_judgment_precedence"],
        },
        "evaluation_policy": {
            "unjudged_documents": "EXCLUDED",
            "unjudged_are_not_not_relevant": True,
            "standard_metrics": "condensed judged-only ranking",
            "documentary_precision": "relevant human qrels / retrieved human-judged qrels",
            "recall_denominator": "all relevant human qrels in the frozen pool",
        },
        "freeze_date": "2026-09-01",
        "corpus_digest": CORPUS_DIGEST,
        "guideline_version": GUIDELINE_VERSION,
        "human_qrels": qrels,
        "human_component_metadata": source["component_judgments"],
        "counts": {
            "documents": dict(sorted(document_counts.items())),
            "metric_components": dict(sorted(component_counts.items())),
            "reviewed_component_metadata": dict(
                sorted(
                    Counter(
                        row["human_label"] for row in source["component_judgments"]
                    ).items()
                )
            ),
        },
        "documents": documents,
        "components": components,
    }
    value["ground_truth_digest"] = canonical_sha256(value)
    return value


def _write_qrels_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "candidate_id",
        "component_id",
        "document_id",
        "occurrence_id",
        "source_entity_id",
        "title",
        "human_label",
        "qrel_grade",
        "review_notes",
        "review_source_file",
        "review_stage",
        "review_sheet",
        "review_row",
        "topic_version",
        "guideline_version",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def freeze(root: Path) -> dict[str, Any]:
    gt2 = root / "benchmarks/discovery/gt2"
    raw_workbook = gt2 / "raw" / PASS3_WORKBOOK
    source_path = gt2 / "orange-fibre-cross-domain-v1-human-review-source.json"
    source = _load(source_path)
    prior = _load(gt2 / "orange-fibre-cross-domain-v1-completeness-candidates.json")
    pass3_import = _load(gt2 / "orange-fibre-cross-domain-v1-pass-3-review-import.json")
    pass3_rows, pass3_audit = _pass3_qrels(pass3_import, prior, raw_workbook)

    stages = [
        (str(stage["stage"]), stage["rows"])
        for stage in source["document_review_stages"]
        if stage["stage"] != "document-review-stage-3"
    ]
    stages.append(("document-review-stage-3", pass3_rows))
    qrels, document_audit = consolidate_document_rows(stages)
    if document_audit["label_distribution"] != {
        "CONTEXTUAL": 57,
        "CORE": 48,
        "NOT_RELEVANT": 245,
    }:
        raise ValueError("unexpected frozen GT2 label distribution")
    component_audit = validate_human_rows(
        source["component_judgments"], identity_field="component_id"
    )

    source["document_review_stages"] = [
        stage
        for stage in source["document_review_stages"]
        if stage["stage"] != "document-review-stage-3"
    ] + [{"stage": "document-review-stage-3", "rows": pass3_rows}]
    source["source_workbooks"] = [
        workbook
        for workbook in source["source_workbooks"]
        if workbook["file"] != PASS3_WORKBOOK
    ] + [
        {
            "file": PASS3_WORKBOOK,
            "human_label_columns_only": True,
            "role": "stage3_completeness",
            "sha256": PASS3_SHA256,
        }
    ]
    source.pop("source_export_sha256", None)
    source["source_export_sha256"] = canonical_sha256(source)

    universe = _full_universe(root)
    completeness, pending = _completeness_control(universe, qrels)
    completeness = _with_artifact_sha256(completeness)
    negative = _load(gt2 / "orange-fibre-cross-domain-v1-negative-control.json")
    negative_complete = (
        negative.get("sample_size") == 60
        and negative.get("distinct_components") == 60
        and negative.get("label_counts") == {"NOT_RELEVANT": 60}
        and negative.get("estimated_residual_miss_signal") == 0.0
    )
    gate = freeze_gate(
        consolidation_audit=document_audit,
        component_audit=component_audit,
        pending_high_priority=pending,
        negative_control_complete=negative_complete,
        guideline_version=GUIDELINE_VERSION,
    )
    if gate["GT2_FREEZE"] != "PASS":
        raise ValueError(f"GT2 freeze remains blocked: {gate}")

    ground_truth = _ground_truth(qrels, source, completeness, universe)
    gate.update(
        {
            "schema_version": 1,
            "artifact_type": "gt2_freeze_gate",
            "case_id": TOPIC_VERSION,
            "completeness_control_completed": True,
            "benchmark_authorized": True,
            "ground_truth_digest": ground_truth["ground_truth_digest"],
            "corpus_digest": CORPUS_DIGEST,
            "freeze_date": "2026-09-01",
            "unjudged_documents_defaulted_to_not_relevant": 0,
        }
    )
    gate = _with_artifact_sha256(gate)
    qrels_artifact = _with_artifact_sha256(
        {
            "schema_version": 1,
            "artifact_type": "gt2_consolidated_qrels_frozen",
            "case_id": TOPIC_VERSION,
            "topic_version": TOPIC_VERSION,
            "guideline_version": GUIDELINE_VERSION,
            "freeze_status": "FROZEN",
            "qrel_mapping": QREL_MAPPING,
            "human_review_stages": [stage["stage"] for stage in source["document_review_stages"]],
            "document_audit": document_audit,
            "component_audit": component_audit,
            "human_qrels": qrels,
            "ground_truth_digest": ground_truth["ground_truth_digest"],
        }
    )
    import_audit = _with_artifact_sha256(
        {
            "schema_version": 1,
            "artifact_type": "gt2_pass_3_import_verification",
            "case_id": TOPIC_VERSION,
            "source_workbook": PASS3_WORKBOOK,
            "source_workbook_sha256": PASS3_SHA256,
            "imported_columns": ["human_label", "review_notes"],
            "candidate_identity_match": True,
            "candidate_count": len(pass3_rows),
            "audit": pass3_audit,
            "non_human_columns_used_as_qrels": False,
        }
    )

    _write_json(source_path, source)
    _write_json(
        gt2 / "orange-fibre-cross-domain-v1-pass-3-import-verification.json", import_audit
    )
    _write_json(
        gt2 / "orange-fibre-cross-domain-v1-consolidated-qrels-frozen.json",
        qrels_artifact,
    )
    _write_qrels_csv(
        gt2 / "orange-fibre-cross-domain-v1-consolidated-qrels-frozen.csv", qrels
    )
    _write_json(
        gt2 / "orange-fibre-cross-domain-v1-completeness-control-final.json", completeness
    )
    _write_json(gt2 / "orange-fibre-cross-domain-v1-freeze-gate.json", gate)
    _write_json(
        root / "benchmarks/discovery/ground_truth/orange-fibre-cross-domain-v1.json",
        ground_truth,
    )
    return {
        "pass3_audit": pass3_audit,
        "document_audit": document_audit,
        "component_audit": component_audit,
        "completeness": completeness,
        "gate": gate,
        "ground_truth_digest": ground_truth["ground_truth_digest"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(freeze(args.root.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
