"""ASTRA-001/002/003/011 isolated acceptance and differential regressions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.opensearch_response import validate_search_response
from services.retrieval_service import (
    ScopeExhaustiveSettings,
    document_content_sha256_from_chunks,
    expand_provenance_graph,
    verify_scope_coverage_certificate,
)
from services.search_service import SearchService
from tests.unit.test_langflow_agent_retrieval_guard import (
    GUARD,
    _ai,
    _complete_coverage,
    _snapshot,
    _tool,
    _user,
)
from tests.unit.test_langflow_retrieval_v2_contract import _load_component_with_langflow_stubs
from tests.unit.test_retrieval_fail_closed import _run_rrf_search
from tests.unit.test_scope_exhaustive_retrieval import (
    _graph_direction,
    _GraphClient,
    _record,
    _successful_retrieval,
)

FULL = {"timed_out": False, "_shards": {"total": 2, "successful": 2, "skipped": 0, "failed": 0}}
PARTIAL = [
    {"timed_out": True},
    {"_shards": {"total": 2, "successful": 1, "skipped": 0, "failed": 1}},
    {"terminated_early": True},
    {"_shards": {"total": 2, "successful": 1, "failed": 0}},
    {"_shards": {"total": 2, "successful": 2, "failed": 0, "failures": [{"reason": "fixture"}]}},
]


async def scope(client, seed, monkeypatch, hidden_resolver=None):
    monkeypatch.setattr("services.search_service.get_index_name", lambda: "documents")
    service = SearchService.__new__(SearchService)
    service.session_manager = SimpleNamespace(get_user_opensearch_client=lambda *_: client)
    service._provenance_hidden_targets = hidden_resolver
    service.search_tool = AsyncMock(return_value=_successful_retrieval([seed["_source"]]))

    async def read(document_id, **kwargs):
        entity_id = kwargs["filters"].get("source_entity_id", ["invalid"])[0]
        chunk = {
            "chunk_id": f"verified-{entity_id}",
            "chunk_index": 0,
            "text": "verified evidence",
            "document_id": document_id,
        }
        chunk["chunk_content_sha256"] = hashlib.sha256(chunk["text"].encode()).hexdigest()
        return {
            "results": [chunk],
            "coverage": {
                "complete": True,
                "covered_chunks": 1,
                "total_chunks": 1,
                "snapshot_sha256": document_content_sha256_from_chunks([chunk]),
            },
        }

    service.read_document_chunks = read
    return await service.search_exhaustive_scope(
        "fixture",
        user_id="visible-user",
        jwt_token="fixture",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )


@pytest.mark.parametrize("mutation", PARTIAL)
def test_001_execution_validation(mutation):
    assert validate_search_response({**FULL, "hits": {"hits": []}, **mutation})


def test_001_execution_evidence_required_and_skipped_shards_are_successful():
    assert validate_search_response({"hits": {"hits": []}})
    assert not validate_search_response(
        {
            **FULL,
            "_shards": {"total": 2, "successful": 2, "failed": 0, "skipped": 2},
            "hits": {"hits": []},
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", PARTIAL[:3])
@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.parametrize("later_page", [False, True])
async def test_001_partial_direction_page_and_identical_observations(
    monkeypatch, mutation, direction, later_page
):
    monkeypatch.setattr("services.retrieval_service.PROVENANCE_GRAPH_PAGE_SIZE", 1)
    records = [
        _record("A", relations=[("reply_to", "B"), ("reply_to", "C")]),
        _record("B", relations=[("reply_to", "A")]),
        _record("C", relations=[("reply_to", "A")]),
    ]

    class PartialClient(_GraphClient):
        injected = 0

        async def search(self, **kwargs):
            response = await super().search(**kwargs)
            body = kwargs["body"]
            if _graph_direction(body) == direction and (not later_page or "search_after" in body):
                response.update(deepcopy(mutation))
                self.injected += 1
            return response

    client = PartialClient(records)
    result = await scope(client, records[0], monkeypatch)
    assert client.injected >= 2  # identical partial executions in both observations
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["graph_execution_complete"] is False
    assert result["coverage"]["graph_execution_failure_codes"]
    assert len(result["documents"]) == 3  # useful verified hits retained
    assert verify_scope_coverage_certificate(result["coverage"])["valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["lexical", "dense"])
@pytest.mark.parametrize("mutation", PARTIAL[:3])
async def test_001_required_lane_partial_retains_hits(monkeypatch, lane, mutation):
    result = await _run_rrf_search(
        monkeypatch, mode="hybrid", embed_fails=False, partial_lane=lane, partial_response=mutation
    )
    assert result["results"]
    assert result["retrieval_execution_complete"] is False
    assert result["effective_retrieval_profile"]["lanes"][lane]["status"] == "failed"


@pytest.mark.asyncio
async def test_001_full_response_preserves_success(monkeypatch):
    a, b = _record("A", relations=[("reply_to", "B")]), _record("B")
    result = await scope(_GraphClient([a, b]), a, monkeypatch)
    assert result["coverage"]["complete"] is True
    assert verify_scope_coverage_certificate(result["coverage"])["valid"]
    assert _snapshot(
        [_user(), _ai("a", "fixture"), _tool("a", coverage=result["coverage"])]
    ).exhaustive_scope_satisfied


def invalidate(source, kind):
    profile = source["source_provenance"]
    if kind == "version":
        profile["schema_version"] = "999"
    elif kind == "identity":
        profile["entity"].pop("id")
    elif kind == "flattened_identity":
        source["source_entity_id"] = "contradiction"
    elif kind == "flattened_type":
        source["source_entity_type"] = "contradiction"
    elif kind == "relation":
        profile["relations"] = [{"target": {"id": "C", "type": "email_message"}}]
    elif kind == "representation":
        profile["relations"] = ["C"]
    elif kind == "target":
        profile["relations"] = [{"role": "reply_to", "target": {"id": "", "type": "email_message"}}]
    elif kind == "projection_relations":
        source["source_relation_roles"] = []
    elif kind == "owner":
        source["owner"] = []


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["seed", "first_hop", "later_hop"])
@pytest.mark.parametrize(
    "kind",
    [
        "version",
        "identity",
        "flattened_identity",
        "flattened_type",
        "relation",
        "representation",
        "target",
        "projection_relations",
        "owner",
    ],
)
async def test_002_provenance_is_validated_in_every_position(monkeypatch, position, kind):
    records = [
        _record("A", relations=[("reply_to", "B")]),
        _record("B", relations=[("reply_to", "C")]),
        _record("C", relations=[("reply_to", "D")]),
        _record("D"),
    ]
    invalid_id = {"seed": "A", "first_hop": "B", "later_hop": "C"}[position]

    class InvalidClient(_GraphClient):
        async def search(self, **kwargs):
            response = await super().search(**kwargs)
            for hit in response["hits"]["hits"]:
                if hit["_source"]["source_entity_id"] == invalid_id:
                    invalidate(hit["_source"], kind)
            return response

    seed = deepcopy(records[0])
    if position == "seed":
        invalidate(seed["_source"], kind)
    result = await scope(InvalidClient(records), seed, monkeypatch)
    assert result["coverage"]["complete"] is False
    assert "provenance_invalid" in result["coverage"]["graph_execution_failure_codes"]
    assert result["coverage"]["provenance_failures"]
    assert verify_scope_coverage_certificate(result["coverage"])["valid"]


def document_hits(count):
    hits = []
    for index in range(count):
        text = f"chunk {index}"
        hits.append(
            {
                "_id": f"physical-{index}",
                "sort": [index, 1, f"chunk-{index}"],
                "_source": {
                    "document_id": "document",
                    "ingest_run_id": "generation-1",
                    "document_profile_version": 1,
                    "document_order_verified": True,
                    "document_chunk_count": count,
                    "source_entity_id": "occurrence",
                    "owner": "reader",
                    "chunk_id": f"chunk-{index}",
                    "chunk_index": index,
                    "page": 1,
                    "text": text,
                    "chunk_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            }
        )
    digest = document_content_sha256_from_chunks([hit["_source"] for hit in hits])
    for hit in hits:
        hit["_source"]["document_content_sha256"] = digest
    return hits


async def read_pages(hits, monkeypatch, *, mutation=None, partial=None, batch_size=1):
    monkeypatch.setattr("services.search_service.get_index_name", lambda: "documents")

    class Client:
        calls = 0

        async def search(self, *, index, body, params):
            self.calls += 1
            page_hits = deepcopy(hits)
            if mutation and self.calls > 1:
                for hit in page_hits:
                    hit["_source"].update(mutation)
            snapshot = page_hits[0]["_source"]["document_content_sha256"]
            cursor = body.get("search_after")
            remaining = [
                hit for hit in page_hits if cursor is None or tuple(hit["sort"]) > tuple(cursor)
            ]
            return {
                **FULL,
                **(partial or {}),
                "hits": {
                    "total": {"value": len(page_hits), "relation": "eq"},
                    "hits": remaining[: body["size"]],
                },
                "aggregations": {"snapshots": {"buckets": [{"key": snapshot}]}},
            }

    client = Client()
    service = SearchService.__new__(SearchService)
    service.session_manager = SimpleNamespace(get_user_opensearch_client=lambda *_: client)
    cursor = ""
    for _ in range(10):
        page = await service.read_document_chunks(
            "document", user_id="reader", jwt_token="fixture", cursor=cursor, batch_size=batch_size
        )
        if page["coverage"]["complete"] or not page["coverage"]["next_cursor"]:
            return page
        cursor = page["coverage"]["next_cursor"]
    pytest.fail("read did not terminate")


@pytest.mark.asyncio
@pytest.mark.parametrize("count,remaining", [(2, [0]), (2, [1]), (3, [0, 2])])
async def test_003_missing_chunks_never_redefine_expected_count(monkeypatch, count, remaining):
    hits = document_hits(count)
    page = await read_pages([hits[i] for i in remaining], monkeypatch, batch_size=3)
    assert page["coverage"]["complete"] is False
    assert page["coverage"]["total_chunks"] == count
    assert "profile" in page["error"] or "order" in page["error"]
    if 0 in remaining:
        assert page["results"]


@pytest.mark.asyncio
async def test_003_duplicate_chunk_index(monkeypatch):
    hits = document_hits(2)
    hits[1]["_source"]["chunk_index"] = 0
    page = await read_pages(hits, monkeypatch, batch_size=2)
    assert page["coverage"]["complete"] is False
    assert "non-contiguous" in page["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"ingest_run_id": "generation-2"},
        {"document_chunk_count": 3},
        {"source_entity_id": "another-occurrence"},
        {"document_id": "other"},
        {"owner": "other"},
    ],
)
async def test_003_profile_cannot_change_across_pages(monkeypatch, mutation):
    page = await read_pages(document_hits(2), monkeypatch, mutation=mutation)
    assert page["coverage"]["complete"] is False
    assert "snapshot_changed" in page["error"]


@pytest.mark.asyncio
async def test_003_document_digest_mismatch(monkeypatch):
    hits = document_hits(2)
    for hit in hits:
        hit["_source"]["document_content_sha256"] = "0" * 64
    page = await read_pages(hits, monkeypatch)
    assert page["coverage"]["complete"] is False
    assert "snapshot digest mismatch" in page["error"]


@pytest.mark.asyncio
async def test_003_complete_document_and_partial_execution(monkeypatch):
    assert (await read_pages(document_hits(3), monkeypatch))["coverage"]["complete"] is True
    page = await read_pages(
        document_hits(2), monkeypatch, partial={"timed_out": True}, batch_size=2
    )
    assert page["results"]
    assert page["coverage"]["complete"] is False
    assert page["coverage"]["next_cursor"] is None


MUTATIONS = [
    ("complete", False),
    ("status_code", "profile_invalid"),
    ("status_message", "edited"),
    ("documents_discovered", 2),
    ("documents_complete", 0),
    ("covered_chunks", 0),
    ("total_chunks", 2),
    ("graph_frontier_empty", False),
    ("graph_limit_reached", True),
    ("graph_stop_reason", "max_documents"),
    ("retrieval_execution_complete", False),
    ("graph_failed", True),
    ("retrieval_failure_codes", ["retrieval_dense_lane_failed"]),
    ("failure_codes", ["profile_invalid"]),
]


@pytest.mark.parametrize("field,value", MUTATIONS)
@pytest.mark.parametrize("rehash", [False, True])
def test_011_differential_field_matrix(field, value, rehash):
    coverage = _complete_coverage()
    coverage[field] = value
    if field in coverage["certification"]["facts"]:
        coverage["certification"]["facts"][field] = value
    if rehash:
        coverage["certification"]["facts_sha256"] = GUARD["_scope_certification_facts_sha256"](
            coverage["certification"]["facts"]
        )
    backend = verify_scope_coverage_certificate(coverage)["valid"]
    assert GUARD["_uses_canonical_coverage_certificate"](coverage) == backend
    assert backend is False
    assert not _snapshot(
        [_user(), _ai("a", "fixture"), _tool("a", coverage=coverage)]
    ).exhaustive_scope_satisfied


@pytest.mark.parametrize(
    "field,value", [("facts_sha256", "0" * 64), ("contract_version", 999), ("contract_id", "other")]
)
def test_011_differential_certificate_envelope(field, value):
    coverage = _complete_coverage()
    coverage["certification"][field] = value
    assert (
        GUARD["_uses_canonical_coverage_certificate"](coverage)
        == verify_scope_coverage_certificate(coverage)["valid"]
        is False
    )


def test_011_differential_lane_status_and_well_formed_incomplete():
    coverage = _complete_coverage()
    coverage.update(
        requested_retrieval_profile={"lanes": {"dense": "required"}},
        effective_retrieval_profile={"lanes": {"dense": {"status": "failed"}}},
    )
    assert (
        GUARD["_uses_canonical_coverage_certificate"](coverage)
        == verify_scope_coverage_certificate(coverage)["valid"]
        is False
    )
    coverage = _complete_coverage()
    facts = coverage["certification"]["facts"]
    facts["graph_frontier_empty"] = False
    coverage["graph_frontier_empty"] = False
    coverage.update(GUARD["certify_scope_coverage"](GUARD["ScopeCertificationFacts"](**facts)))
    assert (
        GUARD["_uses_canonical_coverage_certificate"](coverage)
        == verify_scope_coverage_certificate(coverage)["valid"]
        is True
    )
    assert not _snapshot(
        [_user(), _ai("a", "fixture"), _tool("a", coverage=coverage)]
    ).exhaustive_scope_satisfied


def test_011_embedded_authority_is_exact_source_in_component_and_flow():
    root = Path(__file__).resolve().parents[2]
    contract = (root / "src/services/scope_coverage_contract.py").read_text()
    body = contract[contract.index("SCOPE_COVERAGE_MESSAGES =") :]
    component = (root / "flows/components/openrag_agent.py").read_text()
    assert (
        component.split("# BEGIN GENERATED SCOPE COVERAGE CONTRACT\n")[1].split(
            "# END GENERATED SCOPE COVERAGE CONTRACT"
        )[0]
        == body
    )
    flow = json.loads((root / "flows/openrag_agent.json").read_text())
    agents = [
        n["data"]["node"]
        for n in flow["data"]["nodes"]
        if n["data"]["node"]["display_name"] == "Agent"
    ]
    assert agents and all(agent["template"]["code"]["value"] == component for agent in agents)


@pytest.mark.asyncio
async def test_unproven_reader_absence_does_not_certify_visible_branch(monkeypatch):
    a, b = _record("A", relations=[("reply_to", "B")]), _record("B")
    result = await scope(_GraphClient([a, b], accessible={"A"}), a, monkeypatch)
    assert {d["document_id"] for d in result["documents"]} == {"doc-A"}
    assert "B" not in result["graph"]["entities"]
    assert not result["coverage"]["complete"]
    assert "provenance_target_unresolved" in result["coverage"]["graph_execution_failure_codes"]


@pytest.mark.asyncio
async def test_bounded_max_documents_monotonicity():
    records = [_record("A")] + [_record(f"B{i}", relations=[("reply_to", "A")]) for i in range(5)]
    previous = set()
    for limit in (1, 2, 3, 6, 8):
        result = await expand_provenance_graph(
            _GraphClient(records),
            index_name="documents",
            seed_entity_ids=["A"],
            max_documents=limit,
        )
        reached = {d["document_id"] for d in result["documents"]}
        assert previous <= reached
        previous = reached
    assert len(previous) == 6


@pytest.mark.parametrize("field", list(_complete_coverage()["certification"]["facts"]))
def test_011_malformed_fact_types_fail_closed(field):
    coverage = _complete_coverage()
    coverage["certification"]["facts"][field] = {}
    coverage["certification"]["facts_sha256"] = GUARD["_scope_certification_facts_sha256"](
        coverage["certification"]["facts"]
    )
    assert (
        GUARD["_uses_canonical_coverage_certificate"](coverage)
        == verify_scope_coverage_certificate(coverage)["valid"]
        is False
    )


@pytest.mark.asyncio
async def test_011_failed_seed_execution_has_consistent_certificate(monkeypatch):
    service = SearchService.__new__(SearchService)
    service.search_tool = AsyncMock(
        return_value={
            "error": "search failed",
            "retrieval_execution_complete": False,
            "retrieval_failure_codes": ["retrieval_dense_lane_failed"],
        }
    )
    result = await service.search_exhaustive_scope(
        "fixture",
        user_id="reader",
        jwt_token="fixture",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )
    assert result["coverage"]["complete"] is False
    assert verify_scope_coverage_certificate(result["coverage"])["valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("later_page", [False, True])
async def test_001_metadata_candidate_partial_page_rejected(later_page):
    from services.metadata_candidate_restriction import resolve_metadata_candidates
    from tests.unit.services.test_metadata_candidate_restriction import (
        _DlsSideIndexClient,
        _month_filter,
    )

    class Partial(_DlsSideIndexClient):
        async def search(self, **kwargs):
            response = await super().search(**kwargs)
            if not later_page or kwargs["body"].get("search_after"):
                response["timed_out"] = True
            return response

    with pytest.raises(RuntimeError, match="execution incomplete"):
        await resolve_metadata_candidates(Partial(), _month_filter(), page_size=2)


def test_002_optional_owner_projection_uses_dls_boundary():
    from models.source_provenance import validate_provenance_representative

    source = _record("A")["_source"]
    # Search result transport projects absent optional owner as null. The DLS
    # client, including public/shared access, remains the access authority.
    for owner in (None, "", "another-shared-owner"):
        validate_provenance_representative({**source, "owner": owner})


@pytest.mark.parametrize(
    "source_sha,version",
    [
        ("6e8b8a095928739a9acade2ca017a6a405edb19e", 16),
        ("5147fc3e210cad165cec18cb24239578f3ce539e", 17),
    ],
)
def test_011_managed_migration_preserves_prompt_and_model(source_sha, version):
    import subprocess

    from services.flows_service import FlowsService

    flow = json.loads(
        subprocess.check_output(
            ["git", "show", f"{source_sha}:flows/openrag_agent.json"],
            cwd=Path(__file__).resolve().parents[2],
        )
    )
    flow["id"] = "1098eea1-6649-4e1d-aed1-b77249fb8dd0"
    flow["locked"] = True
    flow["data"]["openrag_retrieval_version"] = version
    agent = next(
        n["data"]["node"]
        for n in flow["data"]["nodes"]
        if n["data"]["node"]["display_name"] == "Agent"
    )
    agent["template"]["system_prompt"]["value"] = "workspace-owned prompt"
    agent["template"]["model"]["value"] = [{"name": "runtime-selected-model", "provider": "OpenAI"}]
    service = FlowsService()
    migrated = service._migrate_known_legacy_retrieval_flow(flow)
    assert migrated is not None
    assert migrated["data"]["openrag_retrieval_version"] == 19
    updated = next(
        n["data"]["node"]
        for n in migrated["data"]["nodes"]
        if n["data"]["node"]["display_name"] == "Agent"
    )
    assert updated["template"]["system_prompt"] == agent["template"]["system_prompt"]
    assert updated["template"]["model"] == agent["template"]["model"]
    assert (
        "return verify_scope_coverage_certificate(coverage)" in updated["template"]["code"]["value"]
    )
    agent["template"]["code"]["value"] += "\n# custom code"
    assert service._migrate_known_legacy_retrieval_flow(flow) is None


@pytest.mark.parametrize("mutation", [None, *MUTATIONS])
def test_011_certificate_survives_actual_tool_and_guard_transport(monkeypatch, mutation):
    module = _load_component_with_langflow_stubs(monkeypatch)
    coverage = _complete_coverage()
    coverage["graph_execution_complete"] = True
    if mutation:
        coverage[mutation[0]] = mutation[1]
    transported = module._model_payload({"results": [], "coverage": coverage})
    # Include JSON serialization and both real projections in the differential
    # path. Empty failure lists are essential to a valid positive certificate.
    state = GUARD["_coverage_state"](json.loads(json.dumps(transported)))
    assert state == coverage
    assert (
        GUARD["_uses_canonical_coverage_certificate"](state)
        == verify_scope_coverage_certificate(coverage)["valid"]
        == (mutation is None)
    )
    assert _snapshot(
        [_user(), _ai("a", "fixture"), _tool("a", coverage=state)]
    ).exhaustive_scope_satisfied == (mutation is None)


def test_011_incomplete_transport_preserves_nulls_and_canonical_failure_order(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    coverage = _complete_coverage()
    for field, value in (
        ("graph_frontier_empty", False),
        ("graph_stop_reason", None),
        ("graph_failed", True),
        ("documents_complete", 0),
    ):
        coverage[field] = value
        coverage["certification"]["facts"][field] = value
    coverage.update(
        GUARD["certify_scope_coverage"](
            GUARD["ScopeCertificationFacts"](**coverage["certification"]["facts"])
        )
    )
    assert len(coverage["failure_codes"]) > 1
    transported = module._model_payload({"results": [], "coverage": coverage})
    state = GUARD["_coverage_state"](json.loads(json.dumps(transported)))
    assert state == coverage
    assert verify_scope_coverage_certificate(state)["valid"]
    assert GUARD["_uses_canonical_coverage_certificate"](state)
    assert not _snapshot(
        [_user(), _ai("a", "fixture"), _tool("a", coverage=state)]
    ).exhaustive_scope_satisfied


@pytest.mark.asyncio
async def test_proven_dls_boundary_certifies_only_accessible_graph(monkeypatch):
    from services.provenance_visibility import resolve_dls_hidden_targets
    from tests.unit.test_provenance_visibility import CountClient

    a, b = _record("A", relations=[("reply_to", "B")]), _record("B")

    async def classify(reader, targets):
        return await resolve_dls_hidden_targets(
            CountClient({"A": 1}), CountClient({"A": 1, "B": 1}), index="documents", targets=targets
        )

    result = await scope(_GraphClient([a, b], accessible={"A"}), a, monkeypatch, classify)
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["documents_discovered"] == 1
    assert result["coverage"]["relations_traversed"]["total"] == 0
    assert result["graph"]["entities"] == ["A"]
    assert result["graph"]["edges"] == []
    assert verify_scope_coverage_certificate(result["coverage"])["valid"]


@pytest.mark.asyncio
async def test_missing_target_still_fails_with_working_visibility_classifier(monkeypatch):
    from services.provenance_visibility import resolve_dls_hidden_targets
    from tests.unit.test_provenance_visibility import CountClient

    a = _record("A", relations=[("reply_to", "missing")])

    async def classify(reader, targets):
        return await resolve_dls_hidden_targets(
            CountClient({"A": 1}), CountClient({"A": 1}), index="documents", targets=targets
        )

    result = await scope(_GraphClient([a]), a, monkeypatch, classify)
    assert result["coverage"]["complete"] is False
    assert "provenance_target_unresolved" in result["coverage"]["graph_execution_failure_codes"]
