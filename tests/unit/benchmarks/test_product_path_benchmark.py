import argparse
import re
from pathlib import Path

from benchmarks.discovery.final_baseline import _view_metrics
from benchmarks.discovery.ground_truth import load_ground_truth
from benchmarks.discovery.product_path_benchmark import (
    _contract_assessment,
    _repetition_plan,
    _request_body,
    compact_product_response,
)
from services.retrieval_service import ScopeCertificationFacts, certify_scope_coverage


def test_repetition_plan_supports_exact_selected_query_counts():
    args = argparse.Namespace(
        query_counts=[1, 4],
        repetitions=3,
        repetition_plan_json='{"1": 5, "4": 10}',
    )

    assert _repetition_plan(args) == {1: 5, 4: 10}


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
        **certify_scope_coverage(
            ScopeCertificationFacts(
                seed_discovery_complete=True,
                seed_documents=1,
                valid_provenance_seed_documents=1,
                invalid_provenance_seed_documents=0,
                graph_frontier_empty=True,
                graph_limit_reached=False,
                graph_stop_reason="frontier_empty",
                graph_failed=False,
                retrieval_execution_complete=True,
                documents_discovered=1,
                documents_complete=1,
                covered_chunks=2,
                total_chunks=2,
            )
        ),
        "seed_discovery_complete": True,
        "seed_documents": 1,
        "valid_provenance_seed_documents": 1,
        "invalid_provenance_seed_documents": 0,
        "documents_discovered": 1,
        "documents_complete": 1,
        "covered_chunks": 2,
        "total_chunks": 2,
        "graph_frontier_empty": True,
        "graph_limit_reached": False,
        "graph_stop_reason": "frontier_empty",
        "graph_failed": False,
        "relations_unclassified": {"total": 0, "by_classification": []},
        "scope_diagnostics": {
            "documents_per_depth": [{"depth": 0, "count": 1}],
            "entities_per_depth": [{"depth": 0, "count": 1}],
            "relations_per_depth": [],
            "largest_expansion_contributors": [],
        },
        "retrieval_execution_complete": True,
        "retrieval_failure_codes": [],
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
            "query_hashes": ["server-q0", "server-q1"],
            "plan_fingerprint": "server-plan",
            "generated_variants": [{"text": "Project Z documents", "kind": "documentary_subject"}],
            "normalized_variants": [
                {
                    "text": "Project Z documents",
                    "normalized_text": "project z documents",
                    "kind": "documentary_subject",
                }
            ],
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


def _runtime_profile() -> dict:
    return {
        "profile_version": 1,
        "status": "MATCH",
        "runtime_behavior_fingerprint": "runtime-fingerprint",
        "planner": {
            "effective_provider": "openai",
            "effective_model": "gpt-test",
            "capability_profile": {
                "registry": "responses-model-capabilities-v1",
                "unsupported_responses_parameters": [],
            },
        },
    }


def test_request_body_uses_product_endpoint_contract_and_one_global_budget():
    q1 = _request_body(
        query="Project Z",
        filters={"owners": ["A"]},
        query_count=1,
        seed_budget=100,
        multi_query_concurrency=2,
    )
    q4 = _request_body(
        query="Project Z",
        filters={"owners": ["A"]},
        query_count=4,
        seed_budget=100,
        multi_query_concurrency=2,
    )

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
        runtime_profile=_runtime_profile(),
    )

    assert run["contract"]["valid"] is True
    assert run["planner"]["model"] == "gpt-test"
    assert run["runtime_behavior_fingerprint"] == "runtime-fingerprint"
    assert run["plan_fingerprint"] == "server-plan"
    assert run["query_hashes"] == ["server-q0", "server-q1"]
    assert run["discovery"]["normalized_variants"][0]["normalized_text"] == ("project z documents")
    assert all("evidence_sha256" in item for item in run["contract"]["validation_evidence"])
    assert run["final_seeds"][0]["source_entity_id"] == "occ-1"
    assert "text" not in run["final_seeds"][0]
    assert "coverage" not in run["scope_identities"][0]
    assert run["coverage"]["scope_diagnostics"]["documents_per_depth"] == [{"depth": 0, "count": 1}]
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
        runtime_profile=_runtime_profile(),
    )
    run["coverage"]["covered_chunks"] = 1
    run["retrieval_execution_complete"] = False

    assessment = _contract_assessment(run)

    assert assessment["valid"] is False
    assert "chunk_counter_mismatch" in assessment["failure_codes"]
    assert "retrieval_execution_incomplete" in assessment["failure_codes"]


def test_benchmark_cannot_bypass_the_canonical_scope_certifier():
    response = _response()
    response["coverage"].pop("certification")
    run = compact_product_response(
        response,
        query="Project Z",
        query_count=2,
        repetition=1,
        seed_budget=100,
        started_at="2026-01-01T00:00:00+00:00",
        http_wall_seconds=3.5,
        runtime_profile=_runtime_profile(),
    )

    assert run["contract"]["valid"] is False
    assert "canonical_certifier:canonical_certification_missing" in (
        run["contract"]["failure_codes"]
    )


def test_generic_contract_renewal_case_loads_without_engine_changes():
    root = Path(__file__).resolve().parents[3]
    definition = load_ground_truth(
        root / "benchmarks/discovery/definitions/case-generic-contract-renewal-v1.yaml"
    )
    seeds = [
        {
            "source_entity_id": "urn:test:contract-alpha:message-1",
            "document_id": "contract-alpha-message-1",
        }
    ]
    closure = {
        "documents": [
            *seeds,
            {
                "source_entity_id": "urn:test:contract-alpha:attachment-1",
                "document_id": "contract-alpha-attachment-1",
            },
        ],
        "coverage": {"complete": True, "status_code": "complete"},
    }

    metrics = _view_metrics(definition, seeds, closure, requested_k=100, labels={"CORE"})

    assert definition["benchmark_id"] == "case-generic-contract-renewal"
    assert {item["human_decision"] for item in definition["documents"]} == {
        "CORE",
        "CONTEXTUAL",
        "NOT_RELEVANT",
    }
    assert {item["human_decision"] for item in definition["components"]} == {
        "CORE",
        "CONTEXTUAL",
        "NOT_RELEVANT",
    }
    assert metrics["seed_component_recall"]["denominator"] == 1
    assert metrics["post_prov_o_document_recall"]["numerator"] == 2
    assert metrics["post_prov_o_document_recall"]["denominator"] == 2


def test_generic_engines_contain_no_surface_case_truth_or_post_hoc_budget():
    root = Path(__file__).resolve().parents[3] / "benchmarks/discovery"
    source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("product_path_benchmark.py", "multi_query_benchmark.py")
    )

    for term in (
        "surface pastorale",
        "pommerieux",
        "ddt",
        "pastoral",
        "administration",
        "préfecture",
        "élevage",
        "abattage",
        "facture",
    ):
        assert term not in source.casefold()
    for historical_literal in (40, 51, 96, 114, 192):
        assert re.search(rf"\b{historical_literal}\b", source) is None
