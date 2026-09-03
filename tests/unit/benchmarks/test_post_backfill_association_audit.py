"""Corpus-audit bounds, cardinality, and cohort contracts."""

from __future__ import annotations

from collections import Counter

from benchmarks.remote_post_backfill_association_audit import (
    _corpus_occurrence_id,
    _format_name,
    bounded_candidate_pairs,
    bucket_statistics,
    classify_bucket_dimension,
    select_cohort,
)


def _candidate(document_id: str, score: str) -> dict[str, str]:
    return {"document_id": document_id, "cohort_score": score}


def test_bucket_statistics_reports_required_percentiles_and_thresholds():
    statistics = bucket_statistics(Counter({"a": 1, "b": 2, "c": 3, "d": 1001}))

    assert statistics == {
        "buckets": 4,
        "count": 1007,
        "min": 1,
        "p50": 3,
        "p75": 1001,
        "p90": 1001,
        "p95": 1001,
        "p99": 1001,
        "max": 1001,
        "singletons": 1,
        "gt_10": 1,
        "gt_50": 1,
        "gt_100": 1,
        "gt_500": 1,
        "gt_1000": 1,
        "gt_5000": 0,
    }


def test_mega_hub_classification_has_deterministic_bounds():
    assert classify_bucket_dimension(bucket_statistics(Counter())) == "NOT_USEFUL_ALONE"
    assert (
        classify_bucket_dimension(bucket_statistics(Counter({"one-source": 47_400})))
        == "NOT_USEFUL_ALONE"
    )
    assert classify_bucket_dimension(Counter(buckets=10, max=1200, p95=1)) == "MEGA_HUB_PRONE"
    assert classify_bucket_dimension(Counter(buckets=10, max=51, p95=1)) == "USABLE_WITH_BOUNDS"
    assert classify_bucket_dimension(Counter(buckets=10, max=10, p95=2)) == "DISCRIMINATING"


def test_candidate_generation_never_enumerates_a_corpus_all_pairs_product():
    members = [f"doc-{index:03d}" for index in range(51)]
    pairs, instrumentation = bounded_candidate_pairs(
        {"SAME_SOURCE_SYSTEM": Counter({"mega": 47_400})},
        {"SAME_SOURCE_SYSTEM": {"mega": members}},
        pair_limit_per_bucket=25,
        global_pair_limit=100,
    )

    assert len(pairs) == 25
    assert instrumentation["candidate_pairs_considered"] == 25
    assert instrumentation["theoretical_pairs_not_enumerated"] > 1_000_000_000
    assert instrumentation["theoretical_pairs_not_enumerated_is_lower_bound"] is True
    assert instrumentation["all_pairs_used"] is False
    assert instrumentation["truncated_dimensions"] == ["SAME_SOURCE_SYSTEM"]


def test_cohort_selection_is_deterministic_and_preserves_strata_before_fill():
    strata = {
        "format:PDF": _candidate("pdf", "10"),
        "format:DOCX": _candidate("docx", "20"),
        "source_type:file": _candidate("file", "30"),
    }
    years = {
        "2020": _candidate("year-2020", "40"),
        "2024": _candidate("year-2024", "50"),
    }
    pool = [_candidate(f"fill-{index}", f"{index + 60:02d}") for index in range(10)]

    first = select_cohort(strata, years, pool, size=8)
    second = select_cohort(strata, years, list(reversed(pool)), size=8)

    assert first == second
    assert {"pdf", "docx", "file", "year-2020", "year-2024"} <= {
        item["document_id"] for item in first
    }
    assert len({item["document_id"] for item in first}) == len(first) == 8


def test_historical_format_strata_prefer_filename_extension_except_eml():
    assert _format_name("application/pdf", "scan.png") == "IMAGE"
    assert _format_name("text/asciidoc", "signature.asc") == "TXT"
    assert _format_name("message/rfc822", "mail.bin") == "EML"


def test_corpus_digest_identity_matches_public_files_contract():
    assert (
        _corpus_occurrence_id({"source_entity_id": "source-1", "document_id": "binary-1"})
        == "source-1"
    )
    assert _corpus_occurrence_id({"document_id": "binary-1"}) == "binary-1"
