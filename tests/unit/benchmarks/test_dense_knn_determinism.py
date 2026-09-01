from benchmarks.discovery.dense_knn_determinism import (
    canonical_sha256,
    summarize_runs,
)


def _run(ids, scores, *, latency=10.0, segment="segment-a"):
    return {
        "request_fingerprint": "request-a",
        "query_vector_sha256": "vector-a",
        "index_uuid": "index-a",
        "segment_snapshot_id": segment,
        "wall_latency_ms": latency,
        "hits": [
            {"chunk_id": identity, "score": score}
            for identity, score in zip(ids, scores, strict=True)
        ],
    }


def test_canonical_sha256_ignores_mapping_insertion_order():
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_summary_separates_membership_rank_and_score_drift():
    runs = [
        _run(["a", "b", "c"], [0.9, 0.8, 0.7]),
        _run(["b", "a", "d"], [0.8, 0.9, 0.6], latency=20.0),
    ]

    summary = summarize_runs(runs)

    assert summary["runs"] == 2
    assert summary["distinct_ordered_result_sets"] == 2
    assert summary["distinct_membership_sets"] == 2
    assert summary["membership_jaccard_min"] == 0.5
    assert summary["max_rank_displacement"] == 1
    assert summary["score_changes_vs_reference"] == 0
    assert summary["distinct_segment_snapshots"] == 1
    assert summary["latency_ms"]["p50"] == 10.0
    assert summary["latency_ms"]["p95"] == 20.0


def test_summary_reports_identical_runs_as_fully_stable():
    runs = [
        _run(["a", "b"], [0.9, 0.8]),
        _run(["a", "b"], [0.9, 0.8]),
    ]

    summary = summarize_runs(runs)

    assert summary["distinct_ordered_result_sets"] == 1
    assert summary["distinct_membership_sets"] == 1
    assert summary["membership_jaccard_min"] == 1.0
    assert summary["rank_correlation_min"] == 1.0
    assert summary["max_rank_displacement"] == 0
    assert summary["score_changes_vs_reference"] == 0
