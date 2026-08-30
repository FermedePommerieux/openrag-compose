"""Dossier-level exhaustive retrieval and PROV-O graph closure contracts."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.retrieval_service import (
    ScopeCertificationFacts,
    ScopeExhaustiveSettings,
    certify_scope_coverage,
    document_content_sha256_from_chunks,
    expand_provenance_graph,
)
from services.search_service import SearchService


def _record(
    entity_id: str,
    *,
    relations: list[tuple[str, str]] | None = None,
    alternate_ids: list[str] | None = None,
) -> dict:
    relation_values = [
        {
            "role": role,
            "target": {"id": target, "type": "document"},
        }
        for role, target in (relations or [])
    ]
    return {
        "_id": f"chunk-{entity_id}",
        "_source": {
            "document_id": f"doc-{entity_id}",
            "filename": f"{entity_id}.eml",
            "chunk_index": 0,
            "source_entity_id": entity_id,
            "source_entity_alternate_ids": alternate_ids or [],
            "source_provenance": {
                "schema_version": "1.0",
                "entity": {
                    "id": entity_id,
                    "type": "email",
                    "alternate_ids": alternate_ids or [],
                },
                "relations": relation_values,
            },
        },
    }


def _provenance(entity_id: str, *, alternate_ids: list[str] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "entity": {
            "id": entity_id,
            "type": "document",
            "alternate_ids": alternate_ids or [],
        },
        "relations": [],
    }


def _seed(document_id: str, entity_id: str) -> dict:
    return {
        "document_id": document_id,
        "source_entity_id": entity_id,
        "source_entity_alternate_ids": [],
        "source_provenance": _provenance(entity_id),
        "chunk_id": f"seed-{entity_id}",
        "text": "seed",
    }


class _GraphClient:
    """Small DLS-aware interpreter for graph query unit tests."""

    def __init__(self, records: list[dict], *, accessible: set[str] | None = None):
        self.records = records
        self.accessible = accessible
        self.bodies: list[dict] = []

    async def search(self, *, index, body, params):
        self.bodies.append(body)
        identity = body["query"]["bool"]["must"][0]
        reverse = "nested" in identity
        if reverse:
            nested_filters = identity["nested"]["query"]["bool"]["filter"]
            roles = set(nested_filters[0]["terms"]["source_provenance.relations.role"])
            target_should = nested_filters[1]["bool"]["should"]
            target_ids = set()
            for clause in target_should:
                target_ids.update(next(iter(clause["terms"].values())))
        else:
            should = identity["bool"]["should"]
            target_ids = set()
            for clause in should:
                target_ids.update(next(iter(clause["terms"].values())))
            roles = set()

        hits = []
        for hit in self.records:
            source = hit["_source"]
            entity_id = source["source_entity_id"]
            if self.accessible is not None and entity_id not in self.accessible:
                continue
            if reverse:
                relations = source["source_provenance"].get("relations", [])
                matched = any(
                    relation["role"] in roles
                    and (
                        relation["target"]["id"] in target_ids
                        or bool(set(relation["target"].get("alternate_ids", [])) & target_ids)
                    )
                    for relation in relations
                )
            else:
                matched = entity_id in target_ids or bool(
                    set(source.get("source_entity_alternate_ids", [])) & target_ids
                )
            if matched:
                hits.append(hit)
        return {
            "hits": {
                "total": {"value": len(hits), "relation": "eq"},
                "hits": hits,
            }
        }


@pytest.mark.asyncio
async def test_graph_traversal_forward_reverse_chain_and_cycle():
    client = _GraphClient(
        [
            _record("A", relations=[("reply_to", "B")]),
            _record("B", relations=[("references", "C")]),
            _record("C", relations=[("reply_to", "A")]),
        ]
    )

    forward = await expand_provenance_graph(client, index_name="documents", seed_entity_ids=["A"])
    reverse = await expand_provenance_graph(client, index_name="documents", seed_entity_ids=["C"])

    assert {item["document_id"] for item in forward["documents"]} == {
        "doc-A",
        "doc-B",
        "doc-C",
    }
    assert {item["document_id"] for item in reverse["documents"]} == {
        "doc-A",
        "doc-B",
        "doc-C",
    }
    assert forward["coverage"]["frontier_empty"] is True
    assert forward["coverage"]["limit_reached"] is False
    assert forward["coverage"]["entities_visited"] == 3


@pytest.mark.asyncio
async def test_reverse_query_keeps_role_and_target_in_same_nested_relation():
    client = _GraphClient(
        [
            _record(
                "A",
                relations=[("references", "wanted"), ("reply_to", "other")],
            ),
            _record("wanted"),
        ]
    )

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=["wanted"],
        allowed_roles=["reply_to"],
    )

    assert {item["document_id"] for item in result["documents"]} == {"doc-wanted"}
    reverse_body = next(
        body for body in client.bodies if "nested" in body["query"]["bool"]["must"][0]
    )
    nested = reverse_body["query"]["bool"]["must"][0]["nested"]
    assert nested["path"] == "source_provenance.relations"
    nested_filters = nested["query"]["bool"]["filter"]
    assert nested_filters[0] == {"terms": {"source_provenance.relations.role": ["reply_to"]}}
    assert nested_filters[1]["bool"]["minimum_should_match"] == 1


@pytest.mark.asyncio
async def test_graph_traversal_uses_alternate_ids_and_dls_view():
    client = _GraphClient(
        [
            _record("A", relations=[("reply_to", "message-b")]),
            _record("B", alternate_ids=["message-b"]),
            _record("hidden", relations=[("reply_to", "A")]),
        ],
        accessible={"A", "B"},
    )

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=["A"],
        filter_clauses=[{"term": {"owner": "user-1"}}],
    )

    assert {item["document_id"] for item in result["documents"]} == {"doc-A", "doc-B"}
    assert "doc-hidden" not in {item["document_id"] for item in result["documents"]}
    assert "hidden" not in result["entities"]
    assert all(
        edge["source_entity_id"] != "hidden" and edge["target_entity_id"] != "hidden"
        for edge in result["edges"]
    )
    assert result["coverage"]["frontier_empty"] is True
    assert "message-b" in result["entities"]
    assert all(
        {"term": {"owner": "user-1"}} in body["query"]["bool"]["filter"] for body in client.bodies
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"max_depth": 1}, "max_depth"),
        ({"max_entities": 1}, "max_entities"),
    ],
)
async def test_graph_limits_never_certify_closure(kwargs, reason):
    client = _GraphClient(
        [
            _record("A", relations=[("reply_to", "B")]),
            _record("B", relations=[("reply_to", "C")]),
            _record("C"),
        ]
    )

    result = await expand_provenance_graph(
        client, index_name="documents", seed_entity_ids=["A"], **kwargs
    )

    assert result["coverage"]["limit_reached"] is True
    assert result["coverage"]["frontier_empty"] is False
    assert result["coverage"]["stop_reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_depth": 1},
        {"max_entities": 1},
        {"max_documents": 1},
    ],
)
async def test_graph_exact_limit_can_close_naturally(kwargs):
    result = await expand_provenance_graph(
        _GraphClient([_record("A")]),
        index_name="documents",
        seed_entity_ids=["A"],
        **kwargs,
    )

    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["limit_reached"] is False
    assert result["coverage"]["stop_reason"] == "frontier_empty"


@pytest.mark.asyncio
async def test_graph_document_limit_requires_an_actual_overflow():
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _record("A", relations=[("reply_to", "B")]),
                _record("B"),
            ]
        ),
        index_name="documents",
        seed_entity_ids=["A"],
        max_documents=1,
    )

    assert result["coverage"]["limit_reached"] is True
    assert result["coverage"]["stop_reason"] == "max_documents"
    assert len(result["documents"]) == 1


@pytest.mark.asyncio
async def test_hidden_relation_target_does_not_consume_entity_limit():
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _record("A", relations=[("reply_to", "hidden")]),
                _record("hidden"),
            ],
            accessible={"A"},
        ),
        index_name="documents",
        seed_entity_ids=["A"],
        max_entities=1,
        max_depth=1,
    )

    assert result["entities"] == ["A"]
    assert result["edges"] == []
    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["limit_reached"] is False


@pytest.mark.asyncio
async def test_graph_two_node_cycle_closes_once():
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _record("A", relations=[("reply_to", "B")]),
                _record("B", relations=[("reply_to", "A")]),
            ]
        ),
        index_name="documents",
        seed_entity_ids=["A"],
    )

    assert result["coverage"]["entities_visited"] == 2
    assert result["coverage"]["frontier_empty"] is True


@pytest.mark.asyncio
async def test_graph_rejects_ambiguous_alternate_identity():
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _record("B", alternate_ids=["shared-id"]),
                _record("C", alternate_ids=["shared-id"]),
            ]
        ),
        index_name="documents",
        seed_entity_ids=["shared-id"],
    )

    assert result["coverage"]["limit_reached"] is True
    assert result["coverage"]["stop_reason"] == "ambiguous_alternate_id"


@pytest.mark.asyncio
async def test_graph_output_is_deterministic_for_reordered_hits():
    records = [
        _record("A", relations=[("reply_to", "B")]),
        _record("B", relations=[("references", "C")]),
        _record("C"),
    ]
    first = await expand_provenance_graph(
        _GraphClient(records), index_name="documents", seed_entity_ids=["A"]
    )
    second = await expand_provenance_graph(
        _GraphClient(list(reversed(records))),
        index_name="documents",
        seed_entity_ids=["A"],
    )

    assert first == second


@pytest.mark.asyncio
async def test_graph_opensearch_error_fails_instead_of_returning_partial_closure():
    client = MagicMock()
    client.search = AsyncMock(side_effect=RuntimeError("OpenSearch unavailable"))

    with pytest.raises(RuntimeError, match="OpenSearch unavailable"):
        await expand_provenance_graph(client, index_name="documents", seed_entity_ids=["A"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_direction", ["forward", "reverse"])
async def test_graph_failure_in_either_direction_fails_closed(failed_direction):
    class DirectionalFailureClient(_GraphClient):
        async def search(self, *, index, body, params):
            identity = body["query"]["bool"]["must"][0]
            direction = "reverse" if "nested" in identity else "forward"
            if direction == failed_direction:
                raise TimeoutError(f"{direction} timeout")
            return await super().search(index=index, body=body, params=params)

    with pytest.raises(TimeoutError, match=f"{failed_direction} timeout"):
        await expand_provenance_graph(
            DirectionalFailureClient([_record("A")]),
            index_name="documents",
            seed_entity_ids=["A"],
        )


def _scope_service() -> SearchService:
    session_manager = MagicMock()
    session_manager.get_user_opensearch_client.return_value = MagicMock()
    return SearchService(session_manager=session_manager)


def _complete_page(document_id: str) -> dict:
    text = f"evidence from {document_id}"
    chunk = {
        "document_id": document_id,
        "filename": f"{document_id}.pdf",
        "chunk_id": f"chunk-{document_id}",
        "chunk_index": 0,
        "chunk_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
    }
    return {
        "results": [chunk],
        "coverage": {
            "mode": "exhaustive",
            "document_id": document_id,
            "filename": f"{document_id}.pdf",
            "covered_chunks": 1,
            "total_chunks": 1,
            "coverage_ratio": 1.0,
            "snapshot_sha256": document_content_sha256_from_chunks([chunk]),
            "complete": True,
            "next_cursor": None,
        },
    }


@pytest.mark.asyncio
async def test_scope_reads_every_discovered_document_and_certifies_only_all_complete(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("doc-A", "A")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(
            return_value={
                "documents": [
                    {"document_id": "doc-A", "filename": "A.pdf"},
                    {"document_id": "doc-B", "filename": "B.pdf"},
                ],
                "entities": ["A", "B"],
                "edges": [{"source_entity_id": "A", "role": "reply_to", "target_entity_id": "B"}],
                "coverage": {
                    "entities_visited": 2,
                    "documents_discovered": 2,
                    "depth_reached": 2,
                    "frontier_empty": True,
                    "limit_reached": False,
                    "stop_reason": "frontier_empty",
                },
            }
        ),
    )
    service.read_document_chunks = AsyncMock(
        side_effect=lambda document_id, **_kwargs: _complete_page(document_id)
    )

    result = await service.search_exhaustive_scope(
        "all exchanges",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["complete"] is True
    assert result["coverage"]["documents_complete"] == 2
    assert result["coverage"]["covered_chunks"] == 2
    assert [item["document_id"] for item in result["results"]] == ["doc-A", "doc-B"]
    assert service.read_document_chunks.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["legacy", "snapshot"])
async def test_scope_is_incomplete_for_legacy_or_changed_document(monkeypatch, failure):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("doc-A", "A")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(
            return_value={
                "documents": [{"document_id": "doc-A", "filename": "A.pdf"}],
                "entities": ["A"],
                "edges": [],
                "coverage": {
                    "entities_visited": 1,
                    "documents_discovered": 1,
                    "depth_reached": 1,
                    "frontier_empty": True,
                    "limit_reached": False,
                    "stop_reason": "frontier_empty",
                },
            }
        ),
    )
    if failure == "legacy":
        service.read_document_chunks = AsyncMock(
            return_value={
                "results": [],
                "error": "reindex it before exhaustive retrieval",
                "coverage": {
                    "mode": "exhaustive",
                    "document_id": "doc-A",
                    "covered_chunks": 0,
                    "total_chunks": 0,
                    "complete": False,
                },
            }
        )
    else:
        service.read_document_chunks = AsyncMock(
            side_effect=RuntimeError("document changed during exhaustive retrieval")
        )

    result = await service.search_exhaustive_scope(
        "all exchanges",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["complete"] is False
    assert result["coverage"]["documents_incomplete"] == 1
    expected = "legacy_document" if failure == "legacy" else "snapshot_changed"
    assert result["coverage"]["status_code"] == expected
    assert expected in result["coverage"]["failure_codes"]
    assert result["documents"][0]["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed_results", "reason"),
    [
        ([], "no_provenance_seed"),
        (
            [{"document_id": "doc-A", "chunk_id": "seed-A", "text": "seed"}],
            "no_provenance_seed",
        ),
    ],
)
async def test_scope_never_certifies_empty_or_unlinked_ranked_seeds(
    monkeypatch, seed_results, reason
):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": seed_results})
    documents = [{"document_id": item["document_id"], "filename": "A.pdf"} for item in seed_results]
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(
            return_value={
                "documents": documents,
                "entities": [],
                "edges": [],
                "coverage": {
                    "entities_visited": 0,
                    "documents_discovered": len(documents),
                    "depth_reached": 0,
                    "frontier_empty": True,
                    "limit_reached": False,
                    "stop_reason": "frontier_empty",
                },
            }
        ),
    )
    service.read_document_chunks = AsyncMock(
        side_effect=lambda document_id, **_kwargs: _complete_page(document_id)
    )

    result = await service.search_exhaustive_scope(
        "all exchanges",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["complete"] is False
    assert result["coverage"]["seed_provenance_complete"] is False
    assert result["coverage"]["status_code"] == reason


def _closed_graph(*document_ids: str) -> dict:
    return {
        "documents": [
            {"document_id": document_id, "filename": f"{document_id}.pdf"}
            for document_id in document_ids
        ],
        "entities": [document_id.removeprefix("doc-") for document_id in document_ids],
        "edges": [],
        "coverage": {
            "entities_visited": len(document_ids),
            "documents_discovered": len(document_ids),
            "depth_reached": 1,
            "frontier_empty": True,
            "limit_reached": False,
            "stop_reason": "frontier_empty",
        },
    }


@pytest.mark.asyncio
async def test_scope_mixed_valid_and_invalid_seeds_is_never_complete(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(
        return_value={
            "results": [
                _seed("doc-A", "A"),
                {"document_id": "doc-B", "chunk_id": "seed-B", "text": "seed"},
            ]
        }
    )
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(return_value=_closed_graph("doc-A", "doc-B")),
    )
    service.read_document_chunks = AsyncMock(
        side_effect=lambda document_id, **_kwargs: _complete_page(document_id)
    )

    result = await service.search_exhaustive_scope(
        "mixed seeds",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["status_code"] == "seed_missing_provenance"
    assert result["coverage"]["valid_provenance_seed_documents"] == 1
    assert result["coverage"]["invalid_provenance_seed_documents"] == 1
    assert result["coverage"]["coverage_ratio"] == 1.0
    assert result["coverage"]["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed_side_effect", "seed_response", "expected"),
    [
        (RuntimeError("search down"), None, "search_error"),
        (None, {"error": "partial seed search"}, "incomplete_seed_discovery"),
    ],
)
async def test_scope_seed_search_failures_have_stable_codes(
    seed_side_effect, seed_response, expected
):
    service = _scope_service()
    if seed_side_effect:
        service.search_tool = AsyncMock(side_effect=seed_side_effect)
    else:
        service.search_tool = AsyncMock(return_value=seed_response)

    result = await service.search_exhaustive_scope(
        "scope",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["status_code"] == expected
    assert isinstance(result["coverage"]["status_message"], str)
    assert expected in result["coverage"]["failure_codes"]


@pytest.mark.asyncio
async def test_scope_retains_partial_evidence_when_one_document_access_fails(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("doc-A", "A")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(return_value=_closed_graph("doc-A", "doc-B")),
    )

    async def read(document_id, **_kwargs):
        if document_id == "doc-B":
            raise RuntimeError("403 access denied")
        return _complete_page(document_id)

    service.read_document_chunks = AsyncMock(side_effect=read)
    result = await service.search_exhaustive_scope(
        "scope",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["status_code"] == "access_error"
    assert result["coverage"]["documents_complete"] == 1
    assert result["coverage"]["documents_incomplete"] == 1
    assert [item["document_id"] for item in result["results"]] == ["doc-A"]
    assert result["documents"][1]["status_code"] == "access_error"


@pytest.mark.asyncio
async def test_scope_recomputes_and_rejects_wrong_final_document_digest(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("doc-A", "A")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(return_value=_closed_graph("doc-A")),
    )
    corrupted = _complete_page("doc-A")
    corrupted["coverage"]["snapshot_sha256"] = "f" * 64
    service.read_document_chunks = AsyncMock(return_value=corrupted)

    result = await service.search_exhaustive_scope(
        "scope",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["status_code"] == "profile_invalid"
    assert result["documents"][0]["complete"] is False
    assert "snapshot digest mismatch" in result["documents"][0]["error"]


@pytest.mark.asyncio
async def test_scope_graph_exception_is_classified_and_seed_document_is_retained(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("doc-A", "A")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(side_effect=RuntimeError("OpenSearch unavailable")),
    )
    service.read_document_chunks = AsyncMock(return_value=_complete_page("doc-A"))

    result = await service.search_exhaustive_scope(
        "scope",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["status_code"] == "graph_traversal_failed"
    assert result["coverage"]["documents_complete"] == 1
    assert [item["document_id"] for item in result["results"]] == ["doc-A"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Document read stopped without a continuation cursor", "cursor_invalid"),
        ("document changed during exhaustive retrieval", "snapshot_changed"),
        ("reindex it before exhaustive retrieval", "legacy_document"),
        ("chunk text digest mismatch", "profile_invalid"),
        ("unexpected transport failure", "document_read_incomplete"),
    ],
)
def test_document_failure_classifier_is_stable(error, expected):
    assert SearchService._scope_document_failure_code(error) == expected


def test_document_ratio_alone_never_certifies_scope():
    decision = certify_scope_coverage(
        ScopeCertificationFacts(
            seed_discovery_complete=True,
            seed_documents=1,
            valid_provenance_seed_documents=1,
            invalid_provenance_seed_documents=0,
            graph_frontier_empty=False,
            graph_limit_reached=True,
            graph_stop_reason="max_depth",
            graph_failed=False,
            documents_discovered=1,
            documents_complete=1,
            covered_chunks=10,
            total_chunks=10,
        )
    )

    assert decision["complete"] is False
    assert decision["status_code"] == "graph_limit_reached"


def test_impossible_coverage_counters_fail_closed():
    decision = certify_scope_coverage(
        ScopeCertificationFacts(
            seed_discovery_complete=True,
            seed_documents=1,
            valid_provenance_seed_documents=1,
            invalid_provenance_seed_documents=0,
            graph_frontier_empty=True,
            graph_limit_reached=False,
            graph_stop_reason="frontier_empty",
            graph_failed=False,
            documents_discovered=1,
            documents_complete=1,
            covered_chunks=2,
            total_chunks=1,
        )
    )

    assert decision["status_code"] == "profile_invalid"
