import pytest

from benchmarks.discovery.multi_query_benchmark import _build_plan, _query_wall_seconds
from benchmarks.discovery.remote_multi_query import _global_fusion, _plan


def _definition() -> dict:
    return {
        "queries": [
            {
                "query_id": "canonical",
                "kind": "canonical_literal",
                "text": "all documents about Project Z",
            }
        ],
        "baseline_run": {
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
        },
        "historical_compatibility": {
            "max_queries": 4,
            "concurrency": 2,
            "final_seed_budget": 96,
        },
        # These labels prove the serialized remote plan excludes evaluator data.
        "documents": [{"occurrence_id": "secret-review-row"}],
        "components": [{"component_id": "secret-component"}],
    }


def test_remote_plan_cannot_contain_ground_truth_or_review_rows():
    plan = _build_plan(_definition())
    assert set(plan) == {
        "query",
        "retrieval",
        "embedding_model",
        "max_queries",
        "concurrency",
        "final_seed_budget",
        "scope",
    }
    assert "secret-review-row" not in repr(plan)
    assert "secret-component" not in repr(plan)


def test_remote_query_plan_keeps_original_deduplicates_and_caps():
    plan = _plan(
        "Decision B",
        '{"queries":['
        '{"text":"decision-b","kind":"conceptual_variant"},'
        '{"text":"appeal Decision B","kind":"administrative_legal"},'
        '{"text":"review Decision B","kind":"relationship_event"},'
        '{"text":"historic Decision B","kind":"historical_wording"}'
        "]}",
        3,
    )
    assert [item["query_id"] for item in plan] == ["q0", "q1", "q2"]
    assert plan[0]["query_text"] == "Decision B"


def test_remote_hierarchical_fusion_has_stable_query_contributions():
    queries = _plan(
        "Project Z",
        '{"queries":[{"text":"Project Z correspondence","kind":"relationship_event"}]}',
        2,
    )

    def hit(chunk_id, query, rank):
        return {
            "chunk_id": chunk_id,
            "document_id": chunk_id,
            "query_contributions": [
                {
                    **query,
                    "query_rrf_rank": rank,
                    "matched_lanes": ["lexical"],
                }
            ],
        }

    fused = _global_fusion(
        [
            (queries[0], [hit("shared", queries[0], 1), hit("a", queries[0], 2)]),
            (queries[1], [hit("shared", queries[1], 1), hit("b", queries[1], 2)]),
        ],
        k=60,
    )
    assert [item["chunk_id"] for item in fused] == ["shared", "a", "b"]
    assert fused[0]["matched_queries"] == ["q0", "q1"]
    assert fused[0]["best_rank_per_query"] == {"q0": 1, "q1": 1}


def test_prefix_latency_uses_bounded_two_query_waves():
    remote = {
        "generation_seconds": 0.5,
        "per_query": [
            {"timings": {"embedding": 1.0, "lexical": 0.1, "dense": 0.2, "fusion": 0.1}},
            {"timings": {"embedding": 2.0, "lexical": 0.1, "dense": 0.2, "fusion": 0.1}},
            {"timings": {"embedding": 3.0, "lexical": 0.1, "dense": 0.2, "fusion": 0.1}},
        ],
    }
    assert _query_wall_seconds(remote, 1) == pytest.approx(1.3)
    assert _query_wall_seconds(remote, 2) == pytest.approx(2.8)
    assert _query_wall_seconds(remote, 3) == pytest.approx(6.1)
