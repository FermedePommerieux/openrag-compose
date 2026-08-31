from benchmarks.discovery.product_path_benchmark import (
    _contract_assessment,
    _request_body,
    compact_product_response,
)


def _profile(*, multi_query: bool = True) -> tuple[dict, dict]:
    requested = {
        "version": 1,
        "strategy": "rrf",
        "mode": "hybrid",
        "lanes": {
            "lexical": "required",
            "dense": "required",
            "fusion": "required",
            "multi_query": "required" if multi_query else "disabled",
        },
    }
    effective = {
        "version": 1,
        "strategy": "rrf",
        "mode": "hybrid",
        "lanes": {
            "lexical": {"requested": True, "status": "succeeded", "candidates": 50},
            "dense": {"requested": True, "status": "succeeded", "candidates": 50},
            "fusion": {"requested": True, "status": "succeeded", "candidates": 96},
            "multi_query": {
                "requested": multi_query,
                "status": "succeeded" if multi_query else "not_requested",
                "candidates": 2 if multi_query else 0,
            },
        },
    }
    return requested, effective


def _response() -> dict:
    requested, effective = _profile()
    coverage = {
        "complete": True,
        "status_code": "complete",
        "failure_codes": [],
        "documents_discovered": 1,
        "documents_complete": 1,
        "covered_chunks": 2,
        "total_chunks": 2,
        "graph_frontier_empty": True,
        "graph_limit_reached": False,
        "retrieval_execution_complete": True,
        "requested_retrieval_profile": requested,
        "effective_retrieval_profile": effective,
        "performance": {
            "discovery_seconds": 1.0,
            "prov_o_seconds": 2.0,
            "total_seconds": 3.0,
        },
    }
    return {
        "results": [{"chunk_id": "all", "text": "must not survive"}],
        "model_results": [
            {
                "chunk_id": "seed-1",
                "document_id": "doc-1",
                "source_entity_id": "occ-1",
                "text": "secret chunk text",
                "matched_lanes": ["lexical", "dense"],
                "query_contributions": [{"query_id": "q0", "query_rrf_rank": 1}],
            }
        ],
        "documents": [
            {
                "document_id": "doc-1",
                "source_entity_id": "occ-1",
                "filename": "one.pdf",
                "complete": True,
                "status_code": "complete",
                "coverage": {"snapshot_sha256": "do-not-copy"},
            }
        ],
        "requested_retrieval_profile": requested,
        "effective_retrieval_profile": effective,
        "retrieval_execution_complete": True,
        "retrieval_failure_codes": [],
        "discovery": {
            "multi_query_requested": True,
            "multi_query_executed": True,
            "multi_query_query_count": 2,
            "multi_query_status": "success",
            "final_seed_chunk_budget": 100,
            "queries": [
                {"query_id": "q0", "query_text": "Project Z", "generation_method": "user"},
                {
                    "query_id": "q1",
                    "query_text": "Project Z documents",
                    "generation_method": "llm_structured_v1",
                },
            ],
        },
        "coverage": coverage,
    }


def test_request_body_uses_product_endpoint_contract_and_one_global_budget():
    q1 = _request_body(query="Project Z", filters={"owners": ["A"]}, query_count=1, seed_budget=100)
    q4 = _request_body(query="Project Z", filters={"owners": ["A"]}, query_count=4, seed_budget=100)

    assert q1["evidenceMode"] == q4["evidenceMode"] == "scope_exhaustive"
    assert q1["limit"] == q4["limit"] == 100
    assert "multiQueryDiscovery" not in q1
    assert q4["multiQueryDiscovery"] is True
    assert q4["multiQueryMaxQueries"] == 4


def test_compact_response_keeps_product_proof_but_never_chunk_text():
    run = compact_product_response(
        _response(),
        query="Project Z",
        query_count=2,
        repetition=1,
        seed_budget=100,
        started_at="2026-01-01T00:00:00+00:00",
        http_wall_seconds=3.5,
        planner={
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "supported_request_parameter_names": ["input", "model", "stream"],
            "actual_request_parameter_names": ["input", "model", "stream"],
        },
    )

    assert run["contract"]["valid"] is True
    assert run["planner"]["temperature_present"] is False
    assert run["final_seeds"][0]["source_entity_id"] == "occ-1"
    assert "text" not in run["final_seeds"][0]
    assert "coverage" not in run["scope_identities"][0]
    assert "must not survive" not in repr(run)
    assert "secret chunk text" not in repr(run)


def test_contract_assessment_fails_closed_on_counter_or_execution_mismatch():
    run = compact_product_response(
        _response(),
        query="Project Z",
        query_count=2,
        repetition=1,
        seed_budget=100,
        started_at="2026-01-01T00:00:00+00:00",
        http_wall_seconds=3.5,
        planner={"actual_request_parameter_names": ["input", "model", "stream"]},
    )
    run["coverage"]["covered_chunks"] = 1
    run["retrieval_execution_complete"] = False

    assessment = _contract_assessment(run)

    assert assessment["valid"] is False
    assert "chunk_counter_mismatch" in assessment["failure_codes"]
    assert "retrieval_execution_incomplete" in assessment["failure_codes"]
