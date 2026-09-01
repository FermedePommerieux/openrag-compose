from benchmarks.discovery.remote_retrieval_correctness import _probe_delta
from benchmarks.discovery.retrieval_correctness_analysis import (
    _planner_analysis,
    _semantic_drift_diagnostics,
    _target_validation_analysis,
)


def _query(query_id: str, text: str) -> dict:
    return {"query_id": query_id, "query_text": text}


def _seed(occurrence: str, chunk: str, queries: list[str]) -> dict:
    return {
        "chunk_id": chunk,
        "source_entity_id": occurrence,
        "matched_queries": queries,
        "query_contributions": [
            {
                "query_id": query_id,
                "matched_lanes": ["lexical", "dense"],
                "global_rrf_contribution": 0.01,
            }
            for query_id in queries
        ],
    }


def _run(query_count: int, run_id: str, plan: list[dict], seeds: list[dict]) -> dict:
    return {
        "run_id": run_id,
        "configuration": {"query_count": query_count},
        "generated_queries": plan,
        "normalized_variants": [
            {"normalized_text": "project z correspondence"},
            {"normalized_text": "project z correspondence"},
        ]
        if query_count == 4
        else [],
        "final_seeds": seeds,
        "scope_identities": seeds,
        "lane_candidates": {"fusion": {"candidates": len(seeds)}},
        "planner": {
            "model": "planner-test",
            "response_model": "planner-test-snapshot",
            "request_fingerprint": "same-request",
            "request_parameters": {"temperature": 0},
        },
        "plan_fingerprint": f"plan-{run_id}",
    }


def test_semantic_drift_is_diagnostic_and_reports_structural_duplicates():
    run = _run(
        4,
        "q4-r1",
        [
            _query("q0", "All records about Project Z"),
            _query("q1", "Project Z correspondence"),
            _query("q2", "Project Z correspondence and replies"),
        ],
        [],
    )
    run["_analysis_id"] = "capture-1:q4-r1"

    result = _semantic_drift_diagnostics(run)

    assert result["automatic_semantic_verdict"] is None
    assert result["duplicate_normalized_variants"] == 1
    assert result["literal_anchors"] == ["project", "z"]
    assert result["variants"][0]["literal_anchors_dropped"] == []


def test_planner_analysis_combines_captures_and_accounts_for_q0_competition():
    q0 = _query("q0", "All records about Project Z")
    q1_seed = _seed("occ-a", "chunk-a", ["q0"])
    q4_plan = [q0, _query("q1", "Project Z correspondence")]
    capture_one = {"runs": [_run(1, "q1-r1", [q0], [q1_seed])]}
    capture_two = {
        "runs": [
            _run(
                4,
                "q4-r1",
                q4_plan,
                [_seed("occ-b", "chunk-b", ["q1"])],
            )
        ]
    }
    definition = {
        "documents": [
            {"occurrence_id": "occ-a", "component_id": "component-a", "human_decision": "CORE"},
            {"occurrence_id": "occ-b", "component_id": "component-b", "human_decision": "CORE"},
        ],
        "components": [
            {"component_id": "component-a", "human_decision": "CORE"},
            {"component_id": "component-b", "human_decision": "CORE"},
        ],
    }

    result = _planner_analysis([capture_one, capture_two], definition)

    assert result["q4_runs"] == 1
    assert result["distinct_request_fingerprints"] == 1
    row = result["q0_representation"][0]
    assert row["relevant_q1_seed_occurrences_evicted"] == 1
    assert row["unique_relevant_variant_occurrences_added"] == 1
    assert row["query_contributions"]["q1"]["matched_seed_count"] == 1


def _observation(
    *, documents: set[str], frontier: set[str], depth: int
) -> dict:
    return {
        "documents": {(document, document) for document in documents},
        "entities": set(documents),
        "edges": set(),
        "frontier": frontier,
        "relations": {},
        "public": {
            "depth": depth,
            "hub_degree": {"observed_hubs": 0, "max": 0, "mean": 0.0},
        },
    }


def test_probe_diagnostics_show_frontier_and_depth_information_beyond_marginal_yield():
    before = _observation(
        documents={"d1", "d2"},
        frontier={"f1", "f2", "f3"},
        depth=2,
    )
    shrinking = _probe_delta(
        before,
        _observation(
            documents={"d1", "d2", "d3"},
            frontier={"f4"},
            depth=2,
        ),
    )
    growing = _probe_delta(
        before,
        _observation(
            documents={"d1", "d2", "d3"},
            frontier={"f4", "f5", "f6", "f7", "f8"},
            depth=3,
        ),
    )

    assert shrinking["marginal_document_yield"] == growing["marginal_document_yield"] == 1
    assert shrinking["frontier_growth_rate"] < 0
    assert growing["frontier_growth_rate"] > 0
    assert shrinking["depth_after"] == 2
    assert growing["depth_after"] == 3


def test_hard_safety_limit_is_aggregated_as_incomplete():
    capture = {
        "result": {
            "documentary_target_validation": [
                {
                    "label": "explosive generic closure",
                    "query_sha256": "query-hash",
                    "fixed_plan_sha256": "",
                    "fixed_strategies": [
                        {
                            "document_limit": 500,
                            "coverage_success": False,
                            "truncated_legitimate_closure": False,
                            "documents": 500,
                            "chunks": 10_000,
                            "graph_latency_seconds": 1.0,
                        }
                    ],
                    "documentary_target_validation": {
                        "state": "HARD_SAFETY_LIMIT_REACHED",
                        "coverage_complete": False,
                        "target_threshold": 250,
                        "validation_probe_size": 50,
                        "hard_safety_limit": 500,
                        "false_target_at_threshold": True,
                        "number_of_probes": 5,
                        "target_extensions": 5,
                        "final_target": 500,
                        "final_observation": {
                            "documents": 500,
                            "entities": 500,
                            "chunks": 10_000,
                            "depth": 5,
                        },
                        "prototype_replay_graph_latency_seconds": 3.0,
                        "probes": [],
                    },
                }
            ]
        }
    }

    result = _target_validation_analysis([capture], [])
    strategy = result["target_validation_strategies"]["target-250-probe-50-hard-500"]

    assert strategy["hard_limit_hits"] == 1
    assert strategy["coverage_success_rate"] == 0
    assert result["natural_closure"]["queries"] == 0
