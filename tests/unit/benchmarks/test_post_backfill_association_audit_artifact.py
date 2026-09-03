"""Integrity gates for the captured read-only production audit artifact."""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AUDIT_PATH = ROOT / "benchmarks/document-metadata/post-backfill-association-audit.json"
REVIEW_PATH = ROOT / "benchmarks/document-metadata/association-neighborhood-human-review.csv"


def _audit() -> dict:
    return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))


def test_corpus_baseline_and_read_only_invariants_are_exact():
    audit = _audit()
    baseline = audit["baseline"]

    assert baseline["distinct_documents"] == 47_400
    assert baseline["visible_occurrences"] == 47_454
    assert baseline["chunks"] == 380_817
    assert baseline["embeddings"] == 380_817
    assert baseline["metadata_profile_occurrences"] == 47_133
    assert baseline["corpus_occurrence_digest"] == (
        "038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7"
    )
    assert baseline["opensearch_before"]["status"] == "green"
    assert baseline["opensearch_after"]["status"] == "green"
    assert audit["invariants"] == {
        "ann_contract": "APPROXIMATE_MEMBERSHIP",
        "coverage_contract_changed": False,
        "deployment": 0,
        "gitops": 0,
        "llm_calls_document_association": 0,
        "llm_calls_metadata_filters": 0,
        "opensearch_mapping_changes": 0,
        "production_metadata_writes": 0,
        "retrieval_config_changes": 0,
        "scope_traversal_changed": False,
    }


def test_association_generation_and_dls_stay_bounded_and_fail_closed():
    audit = _audit()
    population = audit["association_population"]

    assert population["all_pairs_used"] is False
    assert population["largest_bounded_bucket"] == 51
    assert population["unique_candidate_pairs"] <= 20_000
    assert population["theoretical_pairs_not_enumerated_is_lower_bound"] is True
    assert audit["dls_validation"]["result"] == "PASS"
    assert all(
        case["hidden_does_not_affect_candidate_count"]
        and case["hidden_does_not_change_output_or_truncation"]
        and case["hidden_absent_from_output"]
        and not case["hidden_associations_surfaced"]
        for case in audit["dls_validation"]["cases"]
    )


def test_filter_cardinalities_partition_the_distinct_document_corpus():
    for result in _audit()["filters"].values():
        assert result["candidate_count"] == 47_400
        assert result["TRUE"] + result["FALSE"] + result["UNKNOWN"] == 47_400


def test_human_review_artifact_is_not_qrels_and_labels_stay_blank():
    artifact = _audit()["human_review_artifact"]
    rows = list(csv.DictReader(REVIEW_PATH.open(encoding="utf-8")))

    assert artifact["qrels"] is False
    assert len(rows) == artifact["rows"] == 480
    assert all(not row["human_judgment"] and not row["human_note"] for row in rows)
