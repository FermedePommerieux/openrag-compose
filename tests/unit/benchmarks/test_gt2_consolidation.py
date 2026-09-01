import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.discovery.gt2_consolidation import (
    QREL_MAPPING,
    canonical_sha256,
    condensed_standard_ir_metrics,
    consolidate_document_rows,
    freeze_gate,
    generate_title_family_candidates,
    select_negative_control,
    standard_ir_metrics,
    validate_human_rows,
)


def test_human_label_mapping_is_strict_and_graded():
    assert QREL_MAPPING == {"CORE": 2, "CONTEXTUAL": 1, "NOT_RELEVANT": 0}


def test_canonical_digest_is_key_order_independent_and_value_sensitive():
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})


def test_validation_rejects_missing_invalid_duplicate_and_conflicting_labels():
    audit = validate_human_rows(
        [
            {"candidate_id": "a", "human_label": "CORE"},
            {"candidate_id": "a", "human_label": "CONTEXTUAL"},
            {"candidate_id": "b", "human_label": ""},
            {"candidate_id": "c", "human_label": "MAYBE"},
        ],
        identity_field="candidate_id",
    )

    assert audit["valid"] is False
    assert audit["conflicts"] == [{"candidate_id": "a", "human_labels": ["CONTEXTUAL", "CORE"]}]
    assert audit["duplicate_identities"] == ["a"]
    assert audit["empty_labels"] == ["b"]
    assert audit["invalid_labels"] == [{"candidate_id": "c", "human_label": "MAYBE"}]


def test_document_consolidation_is_deterministic_and_rejects_overlap():
    rows, audit = consolidate_document_rows(
        [
            ("stage-1", [{"candidate_id": "b", "human_label": "NOT_RELEVANT"}]),
            ("stage-2", [{"candidate_id": "a", "human_label": "CORE"}]),
        ]
    )

    assert [row["candidate_id"] for row in rows] == ["a", "b"]
    assert audit["valid"] is True
    assert audit["consolidated_qrels_sha256"] == canonical_sha256(rows)

    with pytest.raises(ValueError, match="invalid consolidated"):
        consolidate_document_rows(
            [
                ("stage-1", [{"candidate_id": "a", "human_label": "CORE"}]),
                ("stage-2", [{"candidate_id": "a", "human_label": "CORE"}]),
            ]
        )


def test_negative_control_is_order_independent_and_one_per_component():
    candidates = [
        {"candidate_id": "c", "component_id": "two"},
        {"candidate_id": "a", "component_id": "one"},
        {"candidate_id": "b", "component_id": "one"},
        {"candidate_id": "d", "component_id": "three"},
    ]

    first = select_negative_control(candidates, excluded_candidate_ids={"d"}, sample_size=2)
    second = select_negative_control(
        reversed(candidates), excluded_candidate_ids={"d"}, sample_size=2
    )

    assert first == second
    assert len({row["component_id"] for row in first}) == len(first) == 2
    assert all(row["candidate_id"] != "d" for row in first)


def test_title_family_selection_is_deterministic_and_never_labels_candidates():
    judged = [
        {
            "candidate_id": "anchor",
            "title": "facture_9093277556_2023-04-06.pdf",
            "human_label": "CORE",
        },
        {
            "candidate_id": "not-core",
            "title": "Un intitulé très spécifique",
            "human_label": "CONTEXTUAL",
        },
        {"candidate_id": "generic", "title": "mail--123.eml", "human_label": "CORE"},
    ]
    candidates = [
        {
            "candidate_id": "near",
            "component_id": "x",
            "title": "facture_9073083280_2025-01-06.pdf",
        },
        {"candidate_id": "mail-copy", "component_id": "y", "title": "mail--456.eml"},
        {
            "candidate_id": "context-copy",
            "component_id": "z",
            "title": "Un intitulé très spécifique",
        },
    ]

    first = generate_title_family_candidates(candidates, judged_rows=judged)
    second = generate_title_family_candidates(reversed(candidates), judged_rows=judged)

    assert first == second
    assert [row["candidate_id"] for row in first] == ["near"]
    assert first[0]["human_label"] == ""
    assert first[0]["anchor_candidate_id"] == "anchor"


def test_freeze_gate_is_fail_closed_while_priority_review_is_pending():
    gate = freeze_gate(
        consolidation_audit={"valid": True},
        component_audit={"valid": True},
        pending_high_priority=[{"candidate_id": "pending"}],
        negative_control_complete=True,
        guideline_version="orange-fibre-cross-domain-guideline-v1",
    )

    assert gate == {
        "GT2_FREEZE": "BLOCKED",
        "blockers": ["high_priority_human_review_pending"],
        "pending_high_priority_candidates": 1,
        "negative_control_complete": True,
        "guideline_version": "orange-fibre-cross-domain-guideline-v1",
        "fail_closed": True,
    }


def test_standard_ir_metrics_use_deduplicated_ranked_candidates():
    metrics = standard_ir_metrics(
        ["a", "a", "x", "b"],
        {"a": 2, "b": 1, "missing": 1},
    )

    assert metrics["nDCG@10"] == pytest.approx(0.8472668888)
    assert metrics["nDCG@100"] == pytest.approx(0.8472668888)
    assert metrics["MAP"] == pytest.approx((1.0 + 2 / 3) / 3)
    assert metrics["Recall@100"] == pytest.approx(2 / 3)
    assert metrics["Recall@200"] == pytest.approx(2 / 3)
    assert metrics["Precision@100"] == pytest.approx(0.02)


def test_condensed_standard_metrics_exclude_unjudged_without_demoting_them():
    metrics = condensed_standard_ir_metrics(
        ["unknown-1", "core", "unknown-2", "not-relevant"],
        {"core": 2, "not-relevant": 0, "missing-relevant": 1},
    )

    assert metrics["evaluated_judged_documents"] == 2
    assert metrics["unjudged_documents_excluded"] == 2
    assert metrics["Precision@100"] == pytest.approx(0.5)
    assert metrics["Recall@100"] == pytest.approx(0.5)


def test_versioned_gt2_draft_artifacts_remain_preserved_and_self_consistent():
    root = Path(__file__).resolve().parents[3]
    artifact_dir = root / "benchmarks/discovery/gt2"
    qrels_path = artifact_dir / "orange-fibre-cross-domain-v1-consolidated-qrels-draft.json"
    candidates_path = artifact_dir / "orange-fibre-cross-domain-v1-completeness-candidates.json"
    negative_path = artifact_dir / "orange-fibre-cross-domain-v1-negative-control.json"

    qrels = json.loads(qrels_path.read_text(encoding="utf-8"))
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    negative = json.loads(negative_path.read_text(encoding="utf-8"))

    assert qrels["guideline_version"] == "orange-fibre-cross-domain-guideline-v1"
    assert qrels["freeze_status"] == "NOT_FROZEN"
    assert qrels["document_audit"]["label_distribution"] == {
        "CONTEXTUAL": 56,
        "CORE": 48,
        "NOT_RELEVANT": 233,
    }
    assert len(qrels["human_qrels"]) == 337
    assert len(candidates["candidates"]) == 13
    assert all(row["human_label"] == "" for row in candidates["candidates"])
    assert negative["reproduces_provided_set"] is True
    assert negative["sample_size"] == negative["distinct_components"] == 60
    assert negative["label_counts"] == {"NOT_RELEVANT": 60}
    assert negative["estimated_residual_miss_signal"] == 0.0
    for artifact in (qrels, candidates, negative):
        expected = artifact.pop("artifact_sha256")
        assert canonical_sha256(artifact) == expected


def test_frozen_gt2_artifacts_are_human_only_complete_and_digest_consistent():
    root = Path(__file__).resolve().parents[3]
    artifact_dir = root / "benchmarks/discovery/gt2"
    frozen = json.loads(
        (artifact_dir / "orange-fibre-cross-domain-v1-consolidated-qrels-frozen.json")
        .read_text(encoding="utf-8")
    )
    completeness = json.loads(
        (artifact_dir / "orange-fibre-cross-domain-v1-completeness-control-final.json")
        .read_text(encoding="utf-8")
    )
    gate = json.loads(
        (artifact_dir / "orange-fibre-cross-domain-v1-freeze-gate.json").read_text(
            encoding="utf-8"
        )
    )
    ground_truth = json.loads(
        (root / "benchmarks/discovery/ground_truth/orange-fibre-cross-domain-v1.json")
        .read_text(encoding="utf-8")
    )
    pass3 = json.loads(
        (artifact_dir / "orange-fibre-cross-domain-v1-pass-3-import-verification.json")
        .read_text(encoding="utf-8")
    )
    pass3_import = json.loads(
        (artifact_dir / "orange-fibre-cross-domain-v1-pass-3-review-import.json")
        .read_text(encoding="utf-8")
    )

    assert pass3["imported_columns"] == ["human_label", "review_notes"]
    assert pass3["candidate_count"] == 13
    assert pass3["audit"]["valid"] is True
    assert pass3["audit"]["workbook_values_match"] is True
    assert pass3["audit"]["label_distribution"] == {
        "CONTEXTUAL": 1,
        "NOT_RELEVANT": 12,
    }
    assert len(pass3_import["rows"]) == 13
    assert all(
        set(row) == {"candidate_id", "human_label", "review_notes", "review_row"}
        for row in pass3_import["rows"]
    )
    assert frozen["document_audit"]["valid"] is True
    assert frozen["document_audit"]["duplicate_identities"] == []
    assert frozen["document_audit"]["conflicts"] == []
    assert frozen["document_audit"]["empty_labels"] == []
    assert frozen["document_audit"]["label_distribution"] == {
        "CONTEXTUAL": 57,
        "CORE": 48,
        "NOT_RELEVANT": 245,
    }
    assert len(frozen["human_qrels"]) == 350
    assert completeness["candidate_universe"] == {
        "components": 1338,
        "documents": 3012,
        "judged_documents": 350,
        "unjudged_documents": 2662,
    }
    assert completeness["high_priority_candidates_found"] == 0
    assert completeness["human_review_needed"] == 0
    assert completeness["auto_labels_created"] == 0
    assert gate["GT2_FREEZE"] == "PASS"
    assert gate["benchmark_authorized"] is True
    assert gate["unjudged_documents_defaulted_to_not_relevant"] == 0
    assert ground_truth["evaluation_policy"]["unjudged_are_not_not_relevant"] is True
    assert ground_truth["counts"]["documents"] == {
        "CONTEXTUAL": 57,
        "CORE": 48,
        "NOT_RELEVANT": 245,
    }

    digest = ground_truth.pop("ground_truth_digest")
    assert canonical_sha256(ground_truth) == digest == gate["ground_truth_digest"]
    for artifact in (frozen, completeness, gate, pass3):
        expected = artifact.pop("artifact_sha256")
        assert canonical_sha256(artifact) == expected

    workbook = artifact_dir / "raw/orange-fibre-GT2-completeness-review-pass-3.xlsx"
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == (
        "9745b82639775948aa0a4efcb3ae92f3338a244f1885b898bb1006180cb93fb5"
    )
