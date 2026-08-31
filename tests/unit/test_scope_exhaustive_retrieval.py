"""Dossier-level exhaustive retrieval and PROV-O graph closure contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
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
    entity_type: str = "email_message",
    source_system: str = "openarchiver",
    label: str | None = None,
    generated_at_time: str | None = "2024-01-01T00:00:00Z",
    container_id: str | None = None,
    document_id: str | None = None,
    ingest_run_id: str | None = None,
    chunk_id: str | None = None,
    record_id: str | None = None,
) -> dict:
    target_types = {
        "attachment_of": "email_message",
        "contained_in": "email_archive",
        "member_of": "directory_collection" if entity_type == "file" else "email_thread",
        "references": "email_message",
        "reply_to": "email_message",
    }
    relation_values = []
    for role, target in relations or []:
        relation_values.append(
            {
                "role": role,
                "target": {
                    "id": target,
                    "type": target_types.get(role, "unknown"),
                    "source_system": source_system,
                },
            }
        )
    if container_id:
        relation_values.append(
            {
                "role": "contained_in",
                "target": {
                    "id": container_id,
                    "type": "email_archive",
                    "source_system": source_system,
                },
            }
        )
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
            "source_entity_type": entity_type,
            "source_entity_system": source_system,
            "source_entity_alternate_ids": alternate_ids or [],
            "source_provenance": {
                "schema_version": "1.0",
                "entity": {
                    "id": entity_id,
                    "type": entity_type,
                    "source_system": source_system,
                    "label": label or entity_id,
                    "alternate_ids": alternate_ids or [],
                    "generated_at_time": generated_at_time,
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


def _successful_retrieval(results: list[dict]) -> dict:
    return {
        "results": results,
        "requested_retrieval_profile": {
            "version": 1,
            "strategy": "rrf",
            "mode": "hybrid",
            "lanes": {
                "lexical": "required",
                "dense": "required",
                "fusion": "required",
                "multi_query": "disabled",
            },
        },
        "effective_retrieval_profile": {
            "version": 1,
            "strategy": "rrf",
            "mode": "hybrid",
            "lanes": {
                "lexical": {"status": "succeeded", "candidates": len(results)},
                "dense": {"status": "succeeded", "candidates": len(results)},
                "fusion": {"status": "succeeded", "candidates": len(results)},
                "multi_query": {"status": "not_requested", "candidates": 0},
            },
        },
        "retrieval_execution_complete": True,
        "retrieval_failure_codes": [],
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
        identity_should = identity["bool"]["should"]
        reverse = bool(identity_should and "bool" in identity_should[0])
        if reverse:
            reverse_rules = []
            for policy_clause in identity_should:
                policy_filters = policy_clause["bool"]["filter"]
                source_type = policy_filters[0]["term"]["source_entity_type"]
                nested = policy_filters[1]["nested"]
                nested_filters = nested["query"]["bool"]["filter"]
                role = nested_filters[0]["term"]["source_provenance.relations.role"]
                target_type = nested_filters[1]["term"]["source_provenance.relations.target.type"]
                target_ids = set()
                for clause in nested_filters[2]["bool"]["should"]:
                    target_ids.update(next(iter(clause["terms"].values())))
                reverse_rules.append((role, source_type, target_type, target_ids))
        else:
            target_ids = set()
            for clause in identity_should:
                target_ids.update(next(iter(clause["terms"].values())))
            reverse_rules = []

        hits = []
        for hit in self.records:
            source = hit["_source"]
            entity_id = source["source_entity_id"]
            if self.accessible is not None and entity_id not in self.accessible:
                continue
            if reverse:
                relations = source["source_provenance"].get("relations", [])
                matched = any(
                    source.get("source_entity_type") == source_type
                    and relation["role"] == role
                    and relation["target"].get("type") == target_type
                    and (
                        relation["target"]["id"] in rule_target_ids
                        or bool(set(relation["target"].get("alternate_ids", [])) & rule_target_ids)
                    )
                    for role, source_type, target_type, rule_target_ids in reverse_rules
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
async def test_reverse_query_keeps_role_target_type_and_id_in_same_nested_relation():
    client = _GraphClient(
        [
            _record(
                "A",
                entity_type="email_attachment",
                relations=[("attachment_of", "other"), ("member_of", "wanted")],
            ),
            _record("wanted"),
        ]
    )

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=["wanted"],
    )

    assert {item["document_id"] for item in result["documents"]} == {"doc-wanted"}
    reverse_body = next(body for body in client.bodies if _graph_direction(body) == "reverse")
    policy_clauses = reverse_body["query"]["bool"]["must"][0]["bool"]["should"]
    attachment_clause = next(
        clause
        for clause in policy_clauses
        if clause["bool"]["filter"][0] == {"term": {"source_entity_type": "email_attachment"}}
    )
    nested = attachment_clause["bool"]["filter"][1]["nested"]
    assert nested["path"] == "source_provenance.relations"
    nested_filters = nested["query"]["bool"]["filter"]
    assert nested_filters[0] == {"term": {"source_provenance.relations.role": "attachment_of"}}
    assert nested_filters[1] == {
        "term": {"source_provenance.relations.target.type": "email_message"}
    }
    assert nested_filters[2]["bool"]["minimum_should_match"] == 1


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
    first_clause = identity["bool"]["should"][0]
    return "reverse" if "bool" in first_clause else "forward"


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
async def test_graph_paginates_policy_filtered_reverse_queries(monkeypatch):
    from services import retrieval_service

    monkeypatch.setattr(retrieval_service, "PROVENANCE_GRAPH_PAGE_SIZE", 2)
    client = _GraphClient(
        [_record("A")] + [_record(f"X{index}", relations=[("reply_to", "A")]) for index in range(4)]
    )

    result = await expand_provenance_graph(client, index_name="documents", seed_entity_ids=["A"])

    paginated_directions = {
        _graph_direction(body) for body in client.bodies if "search_after" in body
    }
    assert paginated_directions == {"reverse"}
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
            direction = _graph_direction(body)
            if direction == failed_direction:
                raise TimeoutError(f"{direction} timeout")
            return await super().search(index=index, body=body, params=params)

    with pytest.raises(TimeoutError, match=f"{failed_direction} timeout"):
        await expand_provenance_graph(
            DirectionalFailureClient([_record("A")]),
            index_name="documents",
            seed_entity_ids=["A"],
        )


@pytest.mark.asyncio
async def test_context_archive_is_visible_but_never_reverse_expands():
    archive_id = "archive-OA1"
    records = [
        _record("seed", relations=[("contained_in", archive_id)]),
        *[
            _record(f"archive-{index}", relations=[("contained_in", archive_id)])
            for index in range(50)
        ],
    ]
    client = _GraphClient(records)

    result = await expand_provenance_graph(
        client,
        index_name="documents",
        seed_entity_ids=["seed"],
    )

    assert [document["source_entity_id"] for document in result["documents"]] == ["seed"]
    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["relations_context_only"]["total"] == 1
    assert result["coverage"]["relations_unclassified"]["total"] == 0
    assert result["context_edges"] == [
        {
            "source_entity_id": "seed",
            "role": "contained_in",
            "target_entity_id": archive_id,
            "target_entity_type": "email_archive",
            "target_source_system": "openarchiver",
            "semantics": "contextual",
        }
    ]
    assert result["documents"][0]["scope_context_relations"][0]["target_entity_id"] == archive_id
    assert all(
        "email_archive"
        not in {
            rule["term"].get("source_provenance.relations.target.type")
            for clause in body["query"]["bool"]["must"][0]["bool"]["should"]
            if "bool" in clause
            for policy_filter in clause["bool"]["filter"]
            if "nested" in policy_filter
            for rule in policy_filter["nested"]["query"]["bool"]["filter"]
            if "term" in rule
        }
        for body in client.bodies
        if _graph_direction(body) == "reverse"
    )


@pytest.mark.asyncio
async def test_directory_ingestion_root_is_context_not_scope():
    directory_id = "ingestion-root"
    records = [
        _record(
            "seed-file",
            entity_type="file",
            source_system="local",
            relations=[("member_of", directory_id)],
        ),
        *[
            _record(
                f"file-{index}",
                entity_type="file",
                source_system="local",
                relations=[("member_of", directory_id)],
            )
            for index in range(275)
        ],
    ]

    result = await expand_provenance_graph(
        _GraphClient(records),
        index_name="documents",
        seed_entity_ids=["seed-file"],
    )

    assert len(result["documents"]) == 1
    assert result["coverage"]["frontier_empty"] is True
    assert result["context_edges"][0]["semantics"] == "infrastructure"
    assert result["coverage"]["relations_excluded_by_policy"]["total"] == 2


@pytest.mark.asyncio
async def test_rfc5322_identifier_reconstructs_reference_chain():
    identifier = "urn:openrag:rfc5322:message-id:%3Cmessage-b%40example.test%3E"
    source = _record("A", relations=[("references", identifier)])
    source["_source"]["source_provenance"]["relations"][0]["target"]["type"] = (
        "email_message_identifier"
    )
    target = _record("B", alternate_ids=[identifier])

    result = await expand_provenance_graph(
        _GraphClient([source, target]),
        index_name="documents",
        seed_entity_ids=["A"],
    )

    assert {document["source_entity_id"] for document in result["documents"]} == {"A", "B"}
    assert result["coverage"]["relations_unclassified"]["total"] == 0
    assert result["coverage"]["frontier_empty"] is True


@pytest.mark.asyncio
async def test_unknown_relation_closes_graph_but_fails_policy_certifiability():
    result = await expand_provenance_graph(
        _GraphClient([_record("A", relations=[("new_relation", "B")])]),
        index_name="documents",
        seed_entity_ids=["A"],
    )

    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["limit_reached"] is False
    assert result["coverage"]["relations_unclassified"]["total"] == 2
    assert result["documents"][0]["source_entity_id"] == "A"


@pytest.mark.asyncio
async def test_surface_pastorale_synthetic_archive_thread_and_attachment_regression():
    archive_id = "OA1"
    thread_id = "surface-thread"
    seed = _record(
        "seed-message",
        relations=[("member_of", thread_id), ("contained_in", archive_id)],
    )
    thread_messages = [
        _record(
            f"thread-message-{index}",
            relations=[("member_of", thread_id), ("contained_in", archive_id)],
        )
        for index in range(7)
    ]
    attachment = _record(
        "seed-attachment",
        entity_type="email_attachment",
        relations=[
            ("attachment_of", "seed-message"),
            ("member_of", thread_id),
            ("contained_in", archive_id),
        ],
    )
    archive_members = [
        _record(
            f"archive-only-{index}",
            relations=[("contained_in", archive_id)],
        )
        for index in range(30_000)
    ]

    result = await expand_provenance_graph(
        _GraphClient([seed, attachment, *thread_messages, *archive_members]),
        index_name="documents",
        seed_entity_ids=["seed-message", "seed-attachment"],
    )

    discovered = {document["source_entity_id"] for document in result["documents"]}
    assert discovered == {
        "seed-message",
        "seed-attachment",
        *(f"thread-message-{index}" for index in range(7)),
    }
    assert not any(entity.startswith("archive-only-") for entity in discovered)
    assert result["coverage"]["frontier_empty"] is True
    assert result["coverage"]["limit_reached"] is False
    assert result["coverage"]["relations_unclassified"]["total"] == 0
    assert result["coverage"]["documents_discovered"] == 9


def _rfc_duplicate(
    primary: str,
    alias: str,
    *,
    container: str,
    source_system: str = "openarchiver",
    label: str = "Same message",
    generated_at_time: str = "2024-01-01T00:00:00Z",
) -> dict:
    return _record(
        primary,
        alternate_ids=[alias],
        source_system=source_system,
        label=label,
        generated_at_time=generated_at_time,
        container_id=container,
    )


@pytest.mark.asyncio
async def test_cross_container_rfc_duplicate_occurrences_remain_distinct():
    alias = "urn:openrag:rfc5322:message-id:%3Cshared%40example.test%3E"
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _rfc_duplicate("OA1-message", alias, container="OA1"),
                _rfc_duplicate("OA2-message", alias, container="OA2"),
            ]
        ),
        index_name="documents",
        seed_entity_ids=[alias],
    )

    assert {document["source_entity_id"] for document in result["documents"]} == {
        "OA1-message",
        "OA2-message",
    }
    assert result["coverage"]["identity_shared_aliases_resolved"] == 1
    assert result["coverage"]["limit_reached"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        # Same alternate id claimed twice inside one source container.
        lambda alias: [
            _rfc_duplicate("A", alias, container="OA1"),
            _rfc_duplicate("B", alias, container="OA1"),
        ],
        # Distinct source occurrences with conflicting message evidence.
        lambda alias: [
            _rfc_duplicate("A", alias, container="OA1", source_system="source-A"),
            _rfc_duplicate(
                "B",
                alias,
                container="OA2",
                source_system="source-B",
                label="Different message",
            ),
        ],
        # True collision: matching subject but conflicting timestamp.
        lambda alias: [
            _rfc_duplicate("A", alias, container="OA1"),
            _rfc_duplicate(
                "B",
                alias,
                container="OA2",
                generated_at_time="2024-01-02T00:00:00Z",
            ),
        ],
    ],
)
async def test_ambiguous_alternate_owners_still_fail_closed(records):
    alias = "urn:openrag:rfc5322:message-id:%3Ccollision%40example.test%3E"
    result = await expand_provenance_graph(
        _GraphClient(records(alias)),
        index_name="documents",
        seed_entity_ids=[alias],
    )

    assert result["coverage"]["limit_reached"] is True
    assert result["coverage"]["stop_reason"] == "ambiguous_alternate_id"


@pytest.mark.asyncio
async def test_cross_source_identical_message_is_a_legitimate_duplicate_occurrence():
    alias = "urn:openrag:rfc5322:message-id:%3Ccross-source%40example.test%3E"
    result = await expand_provenance_graph(
        _GraphClient(
            [
                _rfc_duplicate(
                    "source-A-message",
                    alias,
                    container="source-A-container",
                    source_system="source-A",
                ),
                _rfc_duplicate(
                    "source-B-message",
                    alias,
                    container="source-B-container",
                    source_system="source-B",
                ),
            ]
        ),
        index_name="documents",
        seed_entity_ids=[alias],
    )

    assert result["coverage"]["limit_reached"] is False
    assert len(result["documents"]) == 2
    assert len({document["source_entity_id"] for document in result["documents"]}) == 2


@pytest.mark.asyncio
async def test_reingested_same_occurrence_does_not_create_identity_ambiguity():
    alias = "urn:openrag:rfc5322:message-id:%3Creingest%40example.test%3E"
    records = [
        _rfc_duplicate("same-primary", alias, container="OA1"),
        _record(
            "same-primary",
            alternate_ids=[alias],
            container_id="OA1",
            ingest_run_id="second-run",
            record_id="second-physical-hit",
        ),
    ]

    result = await expand_provenance_graph(
        _GraphClient(records),
        index_name="documents",
        seed_entity_ids=[alias],
    )

    assert result["coverage"]["limit_reached"] is False
    assert result["coverage"]["identity_shared_aliases_resolved"] == 0
    assert len(result["documents"]) == 1


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
    service.search_tool = AsyncMock(return_value=_successful_retrieval([_seed("doc-A", "A")]))
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
    assert result["coverage"]["scope_policy_id"] == "documentary-prov-o"
    assert result["coverage"]["scope_policy_version"] == 1
    assert result["coverage"]["relations_unclassified"] == {
        "total": 0,
        "by_classification": [],
    }
    assert result["coverage"]["documents_complete"] == 2
    assert result["coverage"]["covered_chunks"] == 2
    assert [item["document_id"] for item in result["results"]] == ["doc-A", "doc-B"]
    assert service.read_document_chunks.await_count == 2


@pytest.mark.asyncio
async def test_scope_reads_same_content_document_per_source_occurrence(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(
        return_value=_successful_retrieval([_seed("shared-doc", "local")])
    )
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
    service.search_tool = AsyncMock(return_value=_successful_retrieval([_seed("doc-A", "A")]))
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
    service.search_tool = AsyncMock(return_value=_successful_retrieval(seed_results))
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
@pytest.mark.parametrize(
    ("failure_code", "multi_query"),
    [
        ("multi_query_planner_failed", True),
        ("retrieval_dense_lane_failed", False),
    ],
)
async def test_degraded_discovery_preserves_evidence_but_never_certifies(
    monkeypatch, failure_code, multi_query
):
    from services import search_service

    service = _scope_service()
    seed_response = _successful_retrieval([_seed("doc-A", "A")])
    seed_response["retrieval_execution_complete"] = False
    seed_response["retrieval_failure_codes"] = [failure_code]
    seed_response["warnings"] = [{"code": failure_code, "message": "forced sabotage"}]
    if multi_query:
        seed_response["requested_retrieval_profile"]["lanes"]["multi_query"] = "required"
        seed_response["effective_retrieval_profile"]["lanes"]["multi_query"] = {
            "status": "failed",
            "candidates": 1,
            "error": "planner_failed",
        }
        seed_response["discovery"] = {
            "multi_query_requested": True,
            "multi_query_executed": False,
            "multi_query_query_count": 1,
            "multi_query_status": "planner_failed",
        }
        service._search_multi_query = AsyncMock(return_value=seed_response)
    else:
        seed_response["effective_retrieval_profile"]["mode"] = "lexical"
        seed_response["effective_retrieval_profile"]["lanes"]["dense"] = {
            "status": "failed",
            "candidates": 0,
            "error": "embedding_generation_failed",
        }
        seed_response["effective_retrieval_profile"]["lanes"]["fusion"] = {
            "status": "failed",
            "candidates": 0,
            "error": "required_lane_failed",
        }
        service.search_tool = AsyncMock(return_value=seed_response)

    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(return_value=_closed_graph("doc-A")),
    )
    service.read_document_chunks = AsyncMock(return_value=_complete_page("doc-A"))

    result = await service.search_exhaustive_scope(
        "all exchanges",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
        multi_query_discovery=multi_query,
    )

    assert result["results"]
    assert result["coverage"]["documents_complete"] == 1
    assert result["coverage"]["covered_chunks"] == result["coverage"]["total_chunks"]
    assert result["coverage"]["retrieval_execution_complete"] is False
    assert result["coverage"]["complete"] is False
    assert failure_code in result["coverage"]["failure_codes"]
    assert result["warnings"][0]["code"] == failure_code


@pytest.mark.asyncio
async def test_missing_retrieval_execution_contract_fails_closed(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(return_value={"results": [_seed("doc-A", "A")]})
    monkeypatch.setattr(
        search_service,
        "expand_provenance_graph",
        AsyncMock(return_value=_closed_graph("doc-A")),
    )
    service.read_document_chunks = AsyncMock(return_value=_complete_page("doc-A"))

    result = await service.search_exhaustive_scope(
        "all exchanges",
        user_id="user-1",
        jwt_token="jwt",
        filters={},
        embedding_model=None,
        settings=ScopeExhaustiveSettings(),
    )

    assert result["coverage"]["complete"] is False
    assert result["coverage"]["status_code"] == "retrieval_execution_incomplete"
    assert result["coverage"]["retrieval_execution_complete"] is False


@pytest.mark.asyncio
async def test_scope_mixed_valid_and_invalid_seeds_is_never_complete(monkeypatch):
    from services import search_service

    service = _scope_service()
    service.search_tool = AsyncMock(
        return_value=_successful_retrieval(
            [
                _seed("doc-A", "A"),
                {"document_id": "doc-B", "chunk_id": "seed-B", "text": "seed"},
            ]
        )
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
    service.search_tool = AsyncMock(return_value=_successful_retrieval([_seed("doc-A", "A")]))
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
    service.search_tool = AsyncMock(return_value=_successful_retrieval([_seed("doc-A", "A")]))
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
    service.search_tool = AsyncMock(return_value=_successful_retrieval([_seed("doc-A", "A")]))
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
            retrieval_execution_complete=True,
            documents_discovered=1,
            documents_complete=1,
            covered_chunks=10,
            total_chunks=10,
        )
    )

    assert decision["complete"] is False
    assert decision["status_code"] == "graph_limit_reached"


def test_unclassified_relation_has_a_stable_policy_failure_code():
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
            retrieval_execution_complete=True,
            documents_discovered=1,
            documents_complete=1,
            covered_chunks=1,
            total_chunks=1,
            unclassified_relations=1,
        )
    )

    assert decision["complete"] is False
    assert decision["status_code"] == "scope_policy_unclassified_relation"
    assert decision["failure_codes"] == ["scope_policy_unclassified_relation"]


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
            retrieval_execution_complete=True,
            documents_discovered=1,
            documents_complete=1,
            covered_chunks=2,
            total_chunks=1,
        )
    )

    assert decision["status_code"] == "profile_invalid"


def _complete_certification_facts() -> ScopeCertificationFacts:
    return ScopeCertificationFacts(
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


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"covered_chunks": 1}, "document_read_incomplete"),
        ({"covered_chunks": 3}, "profile_invalid"),
        ({"documents_complete": 0}, "document_read_incomplete"),
        ({"documents_complete": 2}, "profile_invalid"),
    ],
)
def test_partition_counter_sabotage_never_certifies(mutation, expected_code):
    decision = certify_scope_coverage(replace(_complete_certification_facts(), **mutation))

    assert decision["complete"] is False
    assert expected_code in decision["failure_codes"]


def test_exact_partition_counters_can_certify():
    decision = certify_scope_coverage(_complete_certification_facts())

    assert decision == {
        "complete": True,
        "status_code": "complete",
        "status_message": decision["status_message"],
        "failure_codes": [],
    }


def test_empty_search_never_certifies_even_with_zero_equalities():
    facts = replace(
        _complete_certification_facts(),
        seed_documents=0,
        valid_provenance_seed_documents=0,
        documents_discovered=0,
        documents_complete=0,
        covered_chunks=0,
        total_chunks=0,
    )

    decision = certify_scope_coverage(facts)

    assert decision["complete"] is False
    assert decision["status_code"] == "no_provenance_seed"


def test_retrieval_execution_is_a_required_certification_input():
    facts = replace(
        _complete_certification_facts(),
        retrieval_execution_complete=False,
        retrieval_failure_codes=("multi_query_planner_failed",),
    )

    decision = certify_scope_coverage(facts)

    assert decision["complete"] is False
    assert decision["status_code"] == "multi_query_planner_failed"
