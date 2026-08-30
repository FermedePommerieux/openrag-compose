"""Dossier-level exhaustive retrieval and PROV-O graph closure contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
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
    document_id: str | None = None,
    ingest_run_id: str | None = None,
    chunk_id: str | None = None,
    record_id: str | None = None,
) -> dict:
    relation_values = [
        {
            "role": role,
            "target": {"id": target, "type": "document"},
        }
        for role, target in (relations or [])
    ]
    resolved_document_id = document_id or f"doc-{entity_id}"
    resolved_ingest_run_id = ingest_run_id or f"run-{entity_id}"
    resolved_chunk_id = chunk_id or f"chunk-{entity_id}"
    return {
        "_id": record_id or f"{resolved_chunk_id}__run_{resolved_ingest_run_id}",
        "_source": {
            "document_id": resolved_document_id,
            "filename": f"{entity_id}.eml",
            "chunk_index": 0,
            "ingest_run_id": resolved_ingest_run_id,
            "chunk_id": resolved_chunk_id,
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
        assert index == "documents"
        assert "pit" not in body
        assert params == {"terminate_after": 0}
        self.bodies.append(deepcopy(body))
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
                hits.append(deepcopy(hit))

        hits.sort(
            key=lambda hit: (
                hit["_source"]["source_entity_id"],
                hit["_source"]["document_id"],
                hit["_source"]["ingest_run_id"],
                hit["_source"]["chunk_id"],
            )
        )
        for hit in hits:
            hit["sort"] = [
                hit["_source"]["source_entity_id"],
                hit["_source"]["document_id"],
                hit["_source"]["ingest_run_id"],
                hit["_source"]["chunk_id"],
            ]
        matched_total = len(hits)

        search_after = body.get("search_after")
        if search_after is not None:
            matching_positions = [
                position for position, hit in enumerate(hits) if hit["sort"] == search_after
            ]
            if len(matching_positions) != 1:
                raise RuntimeError("invalid fake search_after")
            hits = hits[matching_positions[0] + 1 :]

        hits = hits[: int(body["size"])]
        return {
            "hits": {
                "total": {"value": matched_total, "relation": "eq"},
                "hits": hits,
            },
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


def _graph_direction(body: dict) -> str:
    identity = body["query"]["bool"]["must"][0]
    return "reverse" if "nested" in identity else "forward"


@pytest.mark.asyncio
async def test_graph_pagination_single_page_closes_without_pit(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 10)
    client = _GraphClient([_record("A")])

    result = await expand_provenance_graph(client, index_name="documents", seed_entity_ids=["A"])

    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["limit_reached"] is False
    assert result["coverage"]["pagination_pages"] == 4
    assert result["coverage"]["stability_verified"] is True
    assert result["coverage"]["stability_observations"] == 2
    assert all("pit" not in body for body in client.bodies)


@pytest.mark.asyncio
async def test_graph_pagination_multiple_pages_reaches_natural_closure(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)
    client = _GraphClient([_record(entity_id) for entity_id in "ABCDE"])

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=list("ABCDE"),
    )

    forward_pages = [body for body in client.bodies if _graph_direction(body) == "forward"]
    assert len(forward_pages) == 6
    assert [body.get("search_after") for body in forward_pages] == [
        None,
        ["B", "doc-B", "run-B", "chunk-B"],
        ["D", "doc-D", "run-D", "chunk-D"],
        None,
        ["B", "doc-B", "run-B", "chunk-B"],
        ["D", "doc-D", "run-D", "chunk-D"],
    ]
    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["limit_reached"] is False
    assert len(result["documents"]) == 5
    assert result["coverage"]["forward_pages"] == 3
    assert result["coverage"]["forward_verification_pages"] == 3


@pytest.mark.asyncio
async def test_graph_pagination_exact_boundary_does_not_request_empty_page(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)
    client = _GraphClient([_record(entity_id) for entity_id in "ABCD"])

    await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=list("ABCD"),
    )

    forward_pages = [body for body in client.bodies if _graph_direction(body) == "forward"]
    assert len(forward_pages) == 4
    assert [body.get("search_after") for body in forward_pages] == [
        None,
        ["B", "doc-B", "run-B", "chunk-B"],
        None,
        ["B", "doc-B", "run-B", "chunk-B"],
    ]


@pytest.mark.asyncio
async def test_graph_pagination_intermediate_error_fails_closed(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)

    class IntermediateFailureClient(_GraphClient):
        async def search(self, *, index, body, params):
            if _graph_direction(body) == "forward" and body.get("search_after"):
                raise TimeoutError("intermediate page timeout")
            return await super().search(index=index, body=body, params=params)

    client = IntermediateFailureClient([_record(entity_id) for entity_id in "ABC"])
    with pytest.raises(TimeoutError, match="intermediate page timeout"):
        await expand_provenance_graph(
            client,
            index_name="documents",
            seed_entity_ids=list("ABC"),
        )


@pytest.mark.asyncio
async def test_graph_pagination_rejects_invalid_search_after(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)

    class InvalidCursorClient(_GraphClient):
        async def search(self, *, index, body, params):
            response = await super().search(index=index, body=body, params=params)
            if _graph_direction(body) == "forward" and body.get("search_after"):
                response["hits"]["hits"][-1]["sort"] = ["invalid"]
            return response

    with pytest.raises(RuntimeError, match="invalid search_after cursor"):
        await expand_provenance_graph(
            InvalidCursorClient([_record(entity_id) for entity_id in "ABC"]),
            index_name="documents",
            seed_entity_ids=list("ABC"),
        )


@pytest.mark.asyncio
async def test_graph_pagination_rejects_duplicate_between_pages(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)

    class DuplicatePageClient(_GraphClient):
        first_hit_id = ""

        async def search(self, *, index, body, params):
            response = await super().search(index=index, body=body, params=params)
            if _graph_direction(body) != "forward":
                return response
            page_hits = response["hits"]["hits"]
            if body.get("search_after") and page_hits:
                page_hits[0]["_id"] = self.first_hit_id
            elif page_hits:
                self.first_hit_id = page_hits[0]["_id"]
            return response

    with pytest.raises(RuntimeError, match="duplicate paginated hit"):
        await expand_provenance_graph(
            DuplicatePageClient([_record(entity_id) for entity_id in "ABC"]),
            index_name="documents",
            seed_entity_ids=list("ABC"),
        )


@pytest.mark.asyncio
async def test_graph_pagination_rejects_missing_page(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)

    class MissingPageClient(_GraphClient):
        async def search(self, *, index, body, params):
            response = await super().search(index=index, body=body, params=params)
            if _graph_direction(body) == "forward" and body.get("search_after"):
                response["hits"]["hits"] = []
            return response

    with pytest.raises(RuntimeError, match="stopped before completion"):
        await expand_provenance_graph(
            MissingPageClient([_record(entity_id) for entity_id in "ABC"]),
            index_name="documents",
            seed_entity_ids=list("ABC"),
        )


@pytest.mark.asyncio
async def test_graph_pagination_uses_unique_total_sort_key():
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _record(
                    "A",
                    document_id="same-document",
                    ingest_run_id="run-1",
                    chunk_id="same-chunk",
                ),
                _record(
                    "A",
                    document_id="same-document",
                    ingest_run_id="run-2",
                    chunk_id="same-chunk",
                ),
            ]
        ),
        index_name="documents",
        seed_entity_ids=["A"],
    )

    assert result["coverage"]["distinct_results"] == 2
    assert result["coverage"]["stability_verified"] is True


@pytest.mark.asyncio
async def test_graph_pagination_rejects_ambiguous_total_sort_key():
    records = [
        _record(
            "A",
            document_id="same-document",
            ingest_run_id="same-run",
            chunk_id="same-chunk",
            record_id="physical-1",
        ),
        _record(
            "A",
            document_id="same-document",
            ingest_run_id="same-run",
            chunk_id="same-chunk",
            record_id="physical-2",
        ),
    ]

    with pytest.raises(RuntimeError, match="sort key is not unique"):
        await expand_provenance_graph(
            _GraphClient(records),
            index_name="documents",
            seed_entity_ids=["A"],
        )


class _MutationBetweenObservationsClient(_GraphClient):
    def __init__(self, records: list[dict], mutation: str):
        super().__init__(records)
        self.mutation = mutation
        self.forward_observations = 0

    async def search(self, *, index, body, params):
        if _graph_direction(body) == "forward" and "search_after" not in body:
            self.forward_observations += 1
            if self.forward_observations == 2:
                if self.mutation == "insert_before":
                    self.records.append(
                        _record(
                            "A",
                            document_id="aaa-before-cursor",
                            ingest_run_id="inserted-before",
                        )
                    )
                elif self.mutation == "insert_after":
                    self.records.append(
                        _record(
                            "B",
                            document_id="zzz-after-cursor",
                            ingest_run_id="inserted-after",
                        )
                    )
                elif self.mutation == "delete":
                    self.records.pop()
                elif self.mutation == "content":
                    self.records[0]["_source"]["filename"] = "changed.eml"
                elif self.mutation == "relation":
                    self.records[0]["_source"]["source_provenance"]["relations"] = [
                        {
                            "role": "reply_to",
                            "target": {"id": "new-target", "type": "document"},
                        }
                    ]
                elif self.mutation == "identity":
                    self.records[0]["_source"]["source_entity_id"] = "changed-identity"
                else:  # pragma: no cover - protects the test helper contract
                    raise AssertionError(f"Unknown mutation: {self.mutation}")
        return await super().search(index=index, body=body, params=params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["insert_before", "insert_after", "delete", "content", "relation", "identity"],
)
async def test_graph_stability_observation_detects_any_result_change(mutation):
    with pytest.raises(RuntimeError, match="changed between stability observations"):
        await expand_provenance_graph(
            _MutationBetweenObservationsClient(
                [_record("A"), _record("B")],
                mutation,
            ),
            index_name="documents",
            seed_entity_ids=["A", "B"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["before", "after"])
async def test_graph_stability_detects_insertion_during_paginated_scan(monkeypatch, position):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 1)

    class InsertionDuringScanClient(_GraphClient):
        inserted = False

        async def search(self, *, index, body, params):
            if (
                not self.inserted
                and _graph_direction(body) == "forward"
                and body.get("search_after")
            ):
                self.inserted = True
                if position == "before":
                    self.records.append(
                        _record("A", document_id="aaa-before-cursor", ingest_run_id="inserted")
                    )
                else:
                    self.records.append(
                        _record("B", document_id="zzz-after-cursor", ingest_run_id="inserted")
                    )
            return await super().search(index=index, body=body, params=params)

    with pytest.raises(RuntimeError, match="hit total changed during pagination"):
        await expand_provenance_graph(
            InsertionDuringScanClient([_record("A"), _record("B")]),
            index_name="documents",
            seed_entity_ids=["A", "B"],
        )


@pytest.mark.asyncio
async def test_graph_paginates_forward_and_reverse_queries(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)
    client = _GraphClient(
        [_record("A")] + [_record(f"X{index}", relations=[("reply_to", "A")]) for index in range(4)]
    )

    result = await expand_provenance_graph(client, index_name="documents", seed_entity_ids=["A"])

    paginated_directions = {
        _graph_direction(body) for body in client.bodies if "search_after" in body
    }
    assert paginated_directions == {"forward", "reverse"}
    assert result["coverage"]["frontier_empty"] is True
    assert len(result["documents"]) == 5
    assert result["coverage"]["forward_pages"] >= 1
    assert result["coverage"]["reverse_pages"] >= 2
    assert result["coverage"]["forward_verification_pages"] >= 1
    assert result["coverage"]["reverse_verification_pages"] >= 2


@pytest.mark.asyncio
async def test_graph_pagination_preserves_dls_and_active_filters_on_every_page(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 1)
    active_filter = {"term": {"owner": "user-1"}}
    client = _GraphClient(
        [_record(entity_id) for entity_id in "ABCDE"],
        accessible={"A", "B", "C"},
    )

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=list("ABCDE"),
        filter_clauses=[active_filter],
    )

    assert {item["source_entity_id"] for item in result["documents"]} == {"A", "B", "C"}
    assert len(client.bodies) > 2
    assert all(active_filter in body["query"]["bool"]["filter"] for body in client.bodies)
    assert all("pit" not in body for body in client.bodies)


@pytest.mark.asyncio
async def test_graph_business_limits_are_independent_from_technical_pages(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 1)
    client = _GraphClient([_record(entity_id) for entity_id in "ABCDE"])

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=list("ABCDE"),
        max_documents=3,
    )

    assert result["coverage"]["stop_reason"] == "max_documents"
    assert result["coverage"]["limit_reached"] is True
    assert len(result["documents"]) == 3
    assert len([body for body in client.bodies if _graph_direction(body) == "forward"]) == 10


@pytest.mark.asyncio
async def test_graph_paginated_output_is_deterministic_between_executions(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)
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
async def test_graph_keeps_same_content_document_as_distinct_source_occurrences():
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _record("local-source", document_id="shared-document"),
                _record("archive-source", document_id="shared-document"),
            ]
        ),
        index_name="documents",
        seed_entity_ids=["local-source", "archive-source"],
    )

    assert len(result["documents"]) == 2
    assert {item["source_entity_id"] for item in result["documents"]} == {
        "local-source",
        "archive-source",
    }


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
async def test_scope_reads_same_content_document_per_source_occurrence(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("shared-doc", "local")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(
            return_value={
                "documents": [
                    {
                        "document_id": "shared-doc",
                        "filename": "local.pdf",
                        "source_entity_id": "local",
                    },
                    {
                        "document_id": "shared-doc",
                        "filename": "archive.pdf",
                        "source_entity_id": "archive",
                    },
                ],
                "entities": ["archive", "local"],
                "edges": [],
                "coverage": {
                    "entities_visited": 2,
                    "documents_discovered": 2,
                    "depth_reached": 1,
                    "frontier_empty": True,
                    "limit_reached": False,
                    "stop_reason": "frontier_empty",
                },
            }
        ),
    )
    observed_filters: list[dict] = []

    async def read_occurrence(document_id, **kwargs):
        occurrence = kwargs["filters"]["source_entity_id"][0]
        observed_filters.append(kwargs["filters"])
        text = f"evidence from {occurrence}"
        chunk = {
            "document_id": document_id,
            "filename": f"{occurrence}.pdf",
            "chunk_id": f"chunk-{occurrence}",
            "chunk_index": 0,
            "chunk_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text": text,
        }
        return {
            "results": [chunk],
            "coverage": {
                "mode": "exhaustive",
                "document_id": document_id,
                "filename": f"{occurrence}.pdf",
                "covered_chunks": 1,
                "total_chunks": 1,
                "coverage_ratio": 1.0,
                "snapshot_sha256": document_content_sha256_from_chunks([chunk]),
                "complete": True,
                "next_cursor": None,
            },
        }

    service.read_document_chunks = AsyncMock(side_effect=read_occurrence)
    result = await service.search_exhaustive_scope(
        "all exchanges",
        user_id="user-1",
        jwt_token="jwt",
        filters={"connector_types": ["filesystem", "openarchiver"]},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["complete"] is True
    assert result["coverage"]["documents_discovered"] == 2
    assert result["coverage"]["documents_complete"] == 2
    assert {item["chunk_id"] for item in result["results"]} == {
        "chunk-local",
        "chunk-archive",
    }
    assert {item["source_entity_id"][0] for item in observed_filters} == {
        "local",
        "archive",
    }
    assert all(
        item["connector_types"] == ["filesystem", "openarchiver"] for item in observed_filters
    )


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
    assert result["coverage"]["graph_error"] == "OpenSearch unavailable"
    assert result["coverage"]["graph_stability_verified"] is False
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
