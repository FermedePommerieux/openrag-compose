import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.retrieval_transport import project_scope_exhaustive_for_langflow


def _chunk(index: int, *, text_size: int = 2048) -> dict:
    return {
        "document_id": f"doc-{index % 250}",
        "source_entity_id": f"occurrence-{index % 250}",
        "chunk_id": f"chunk-{index}",
        "filename": f"document-{index % 250}.pdf",
        "source_url": f"/api/source-files/doc-{index % 250}.token",
        "text": f"EXHAUSTIVE-{index}-" + ("x" * text_size),
    }


def _scope_payload(chunk_count: int, *, complete: bool = False, text_size: int = 2048) -> dict:
    results = [_chunk(index, text_size=text_size) for index in range(chunk_count)]
    model_results = results[:96]
    return {
        "results": results,
        "model_results": model_results,
        "total": chunk_count,
        "documents": [
            {
                "document_id": f"doc-{index}",
                "source_entity_id": f"occurrence-{index}",
                "scope_context_relations": [
                    {
                        "role": "contained_in",
                        "target_entity_id": "archive-OA1",
                        "target_entity_type": "email_archive",
                        "semantics": "contextual",
                    }
                ],
                "filename": f"document-{index}.pdf",
                "complete": True,
                "status_code": "complete",
                "coverage": {"snapshot_sha256": "f" * 64},
                "source_provenance": {"large": "not-for-langflow" * 50},
            }
            for index in range(250)
        ],
        "evidence_batches": [{"chunk_ids": [f"chunk-{index}" for index in range(chunk_count)]}],
        "graph": {
            "entities": [f"entity-{index}" for index in range(chunk_count)],
            "edges": [{"source": index, "target": index + 1} for index in range(chunk_count)],
        },
        "coverage": {
            "mode": "scope_exhaustive",
            "scope_policy_id": "documentary-prov-o",
            "scope_policy_version": 1,
            "complete": complete,
            "status_code": "complete" if complete else "document_limit_reached",
            "status_message": "certificate authored by the backend",
            "failure_codes": [] if complete else ["document_limit_reached"],
            "documents_discovered": 250,
            "documents_complete": 250,
            "covered_chunks": chunk_count,
            "total_chunks": chunk_count,
            "document_read_coverage_ratio": 1.0,
            "graph_reverse_hits": 47_451,
            "graph_stability_verified": True,
            "model_evidence_chunks": len(model_results),
            "artifact_chunks": chunk_count,
        },
        "warnings": [{"code": "retrieval_dense_lane_failed", "message": "partial"}],
        "requested_retrieval_profile": {"version": 1, "mode": "hybrid"},
        "effective_retrieval_profile": {"version": 1, "mode": "lexical"},
        "retrieval_execution_complete": False,
        "retrieval_failure_codes": ["retrieval_dense_lane_failed"],
    }


@pytest.mark.parametrize("complete", [False, True])
def test_langflow_scope_projection_preserves_backend_coverage_and_occurrences(complete):
    payload = _scope_payload(100, complete=complete)
    compact = project_scope_exhaustive_for_langflow(payload)

    assert compact["coverage"] == payload["coverage"]
    assert compact["coverage"] is not payload["coverage"]
    assert compact["coverage"]["scope_policy_id"] == "documentary-prov-o"
    assert compact["coverage"]["scope_policy_version"] == 1
    assert len(compact["results"]) == 96
    assert compact["documents"][0]["source_entity_id"] == "occurrence-0"
    assert compact["documents"][1]["source_entity_id"] == "occurrence-1"
    assert compact["documents"][0]["scope_context_relations"][0]["semantics"] == "contextual"
    assert "coverage" not in compact["documents"][0]
    assert "source_provenance" not in compact["documents"][0]
    assert "model_results" not in compact
    assert "evidence_batches" not in compact
    assert "graph" not in compact
    assert compact["transport"] == {
        "profile": "langflow",
        "scope_evidence_omitted": True,
        "source_resolution": "dls_chunk_id",
    }
    assert compact["warnings"] == payload["warnings"]
    assert compact["retrieval_execution_complete"] is False
    assert compact["requested_retrieval_profile"]["mode"] == "hybrid"
    assert compact["effective_retrieval_profile"]["mode"] == "lexical"


def test_langflow_scope_payload_is_independent_of_verified_chunk_text_volume():
    small = _scope_payload(100)
    large = _scope_payload(10_000)

    small_old_size = len(json.dumps(small, separators=(",", ":")))
    large_old_size = len(json.dumps(large, separators=(",", ":")))
    small_compact = project_scope_exhaustive_for_langflow(small)
    large_compact = project_scope_exhaustive_for_langflow(large)
    small_size = len(json.dumps(small_compact, separators=(",", ":")))
    large_size = len(json.dumps(large_compact, separators=(",", ":")))

    # The legacy payload includes a fixed 250-document manifest in both cases,
    # so its total ratio is below 100 even though chunk text itself grows x100.
    assert large_old_size > small_old_size * 25
    assert large_size < small_size * 1.01
    assert len(large_compact["results"]) == 96
    assert "EXHAUSTIVE-9999" not in json.dumps(large_compact)


def test_surface_pastorale_regression_keeps_coverage_but_only_96_source_chunks():
    payload = _scope_payload(9_069)
    compact = project_scope_exhaustive_for_langflow(payload)

    assert compact["coverage"]["documents_complete"] == 250
    assert compact["coverage"]["covered_chunks"] == 9_069
    assert compact["coverage"]["document_read_coverage_ratio"] == 1.0
    assert compact["coverage"]["status_code"] == "document_limit_reached"
    assert len(compact["results"]) == 96
    assert len(compact["documents"]) == 250


def test_langflow_transport_remains_bounded_above_largest_calibration_chunk_count():
    payload = _scope_payload(25_000, text_size=64)

    compact = project_scope_exhaustive_for_langflow(payload)

    assert len(compact["results"]) == 96
    assert "evidence_batches" not in compact
    assert "graph" not in compact
    assert "EXHAUSTIVE-24999" not in json.dumps(compact)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence_mode", "response_profile"),
    [
        ("focused", "langflow"),
        ("exhaustive", "langflow"),
        ("scope_exhaustive", "default"),
    ],
)
async def test_search_api_keeps_historical_payloads_unchanged(evidence_mode, response_profile):
    from api.search import SearchBody, search

    original = {
        "results": [_chunk(0)],
        "model_results": [_chunk(0)],
        "coverage": {"complete": True},
        "graph": {"entities": ["kept-for-compatible-callers"]},
    }
    service = SimpleNamespace(search=AsyncMock(return_value=original))
    body = SearchBody(
        query="question",
        evidenceMode=evidence_mode,
        documentId="doc-0" if evidence_mode == "exhaustive" else None,
        responseProfile=response_profile,
    )

    response = await search(
        body,
        search_service=service,
        session_manager=object(),
        user=SimpleNamespace(user_id="user-1", jwt_token="jwt"),
    )

    assert json.loads(response.body) == original


@pytest.mark.asyncio
async def test_search_api_applies_langflow_profile_only_to_scope_exhaustive():
    from api.search import SearchBody, search

    original = _scope_payload(1_000)
    service = SimpleNamespace(search=AsyncMock(return_value=original))
    response = await search(
        SearchBody(
            query="all exchanges",
            evidenceMode="scope_exhaustive",
            responseProfile="langflow",
        ),
        search_service=service,
        session_manager=object(),
        user=SimpleNamespace(user_id="user-1", jwt_token="jwt"),
    )
    payload = json.loads(response.body)

    assert len(payload["results"]) == 96
    assert payload["coverage"] == original["coverage"]
    assert "graph" not in payload
    assert "evidence_batches" not in payload
