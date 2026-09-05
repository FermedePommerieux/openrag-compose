from __future__ import annotations

from typing import Any

import pytest

from models.document_investigation import CalendarBasis
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterBooleanOperator,
    MetadataFilterClause,
    MetadataFilterExpression,
    MetadataFilterField,
    MetadataFilterOperator,
)
from models.structured_document_query import (
    MetadataCandidateDiagnostics,
    MetadataCandidateRestriction,
    StructuredDocumentQuery,
)
from services.metadata_candidate_restriction import (
    METADATA_RETRIEVAL_TERMS_BATCH,
    candidate_id_partitions,
    execute_metadata_restricted_lane,
    resolve_metadata_candidates,
    restrict_lane_body,
    validate_metadata_filter_complexity,
)


def _month_filter() -> MetadataFilter:
    return MetadataFilter(
        clauses=(
            MetadataFilterClause(
                field=MetadataFilterField.PRODUCTION_MONTH,
                operator=MetadataFilterOperator.EQUAL,
                values=("2024-03",),
                calendar_basis=CalendarBasis.SOURCE_LOCAL,
                source_policy=(MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION),
            ),
        )
    )


def _restriction(ids: tuple[str, ...]) -> MetadataCandidateRestriction:
    metadata_filter = _month_filter()
    return MetadataCandidateRestriction(
        source_entity_ids=ids,
        projection_alias="documents-metadata-filter-current",
        diagnostics=MetadataCandidateDiagnostics(
            filter_sha256=metadata_filter.calculate_sha256(),
            filters_requested=1,
            filters_effective=1,
            visible_projection_count=len(ids),
            eligible_count=len(ids),
            pages=1,
        ),
    )


class _DlsSideIndexClient:
    def __init__(self) -> None:
        self.search_bodies: list[dict[str, Any]] = []

    async def count(self, *, index: str) -> dict[str, int]:
        assert index == "documents-metadata-filter-current"
        return {"count": 3}

    async def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        assert index == "documents-metadata-filter-current"
        self.search_bodies.append(body)
        assert "filter.projection_sha256" in str(body["query"])
        if "search_after" not in body:
            return {
                "timed_out": False,
                "_shards": {"total": 1, "successful": 1, "failed": 0},
                "hits": {
                    "total": {"value": 2, "relation": "eq"},
                    "hits": [
                        {
                            "_source": {"source_entity_id": "visible-b"},
                            "sort": ["visible-b", "doc-b", "projection-b"],
                        },
                        {
                            "_source": {"source_entity_id": "visible-a"},
                            "sort": ["visible-a", "doc-a", "projection-a"],
                        },
                    ],
                },
            }
        return {
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": 2, "relation": "eq"}, "hits": []},
        }


@pytest.mark.asyncio
async def test_candidate_resolution_is_dls_scoped_paginated_and_canonical():
    client = _DlsSideIndexClient()

    result = await resolve_metadata_candidates(client, _month_filter(), page_size=2)

    assert result.source_entity_ids == ("visible-a", "visible-b")
    assert result.diagnostics.visible_projection_count == 3
    assert result.diagnostics.eligible_count == 2
    assert result.diagnostics.pages == 2
    assert "hidden" not in result.model_dump_json()


def test_structured_query_keeps_free_text_and_metadata_separate():
    query = StructuredDocumentQuery(
        free_text="factures Orange",
        metadata_filter=_month_filter(),
    )

    assert query.free_text == "factures Orange"
    assert query.metadata_filter == _month_filter()
    assert len(query.calculate_sha256()) == 64


def test_internal_or_unsafe_filter_field_is_rejected_fail_closed():
    metadata_filter = MetadataFilter(
        clauses=(
            MetadataFilterClause(
                field=MetadataFilterField.FILENAME_BASENAME,
                operator=MetadataFilterOperator.EQUAL,
                values=("facture",),
            ),
        )
    )

    with pytest.raises(ValueError, match="unsupported_filter"):
        validate_metadata_filter_complexity(metadata_filter)


def test_filter_complexity_bounds_in_values_and_depth():
    too_many_values = MetadataFilter(
        clauses=(
            MetadataFilterClause(
                field=MetadataFilterField.SOURCE_SYSTEM,
                operator=MetadataFilterOperator.IN,
                values=tuple(f"source-{index}" for index in range(65)),
            ),
        )
    )
    with pytest.raises(ValueError, match="at most 64"):
        validate_metadata_filter_complexity(too_many_values)

    expression = MetadataFilterExpression(clause=_month_filter().clauses[0])
    for _ in range(4):
        expression = MetadataFilterExpression(
            operator=MetadataFilterBooleanOperator.NOT,
            children=(expression,),
        )
    with pytest.raises(ValueError, match="nesting depth"):
        validate_metadata_filter_complexity(MetadataFilter(expression=expression))


def test_candidate_partition_is_bounded_and_lane_injection_is_exact():
    ids = tuple(f"entity-{index:04d}" for index in range(700))
    partitions = candidate_id_partitions(ids)

    assert len(partitions) == 2
    assert max(map(len, partitions)) == METADATA_RETRIEVAL_TERMS_BATCH
    lexical = restrict_lane_body(
        {"query": {"bool": {"must": [{"match": {"text": "orange"}}]}}, "size": 50},
        partitions[0],
    )
    dense = restrict_lane_body(
        {"query": {"bool": {"should": [{"knn": {"vector": {}}}]}}, "size": 50},
        partitions[0],
    )
    expected = {"terms": {"source_entity_id": list(partitions[0])}}
    assert lexical["query"]["bool"]["filter"][-1] == expected
    assert dense["query"]["bool"]["filter"][-1] == expected


@pytest.mark.asyncio
async def test_lexical_and_dense_lane_executor_use_every_same_partition():
    ids = tuple(f"entity-{index:04d}" for index in range(600))
    restriction = _restriction(ids)
    observed: dict[str, list[tuple[str, ...]]] = {"lexical": [], "dense": []}

    async def execute(lane: str, body: dict[str, Any]) -> dict[str, Any]:
        terms = tuple(body["query"]["bool"]["filter"][-1]["terms"]["source_entity_id"])
        observed[lane].append(terms)
        index = len(observed[lane])
        return {
            "hits": {
                "hits": [
                    {
                        "_id": f"{lane}-{index}",
                        "_score": float(10 - index),
                        "_source": {"chunk_id": f"{lane}-{index}"},
                    }
                ]
            }
        }

    lexical = await execute_metadata_restricted_lane(
        {"query": {"bool": {"must": [{"match": {"text": "orange"}}]}}, "size": 50},
        restriction,
        execute=lambda body: execute("lexical", body),
    )
    dense = await execute_metadata_restricted_lane(
        {"query": {"bool": {"should": [{"knn": {"vector": {}}}]}}, "size": 50},
        restriction,
        execute=lambda body: execute("dense", body),
    )

    assert observed["lexical"] == observed["dense"]
    assert len(observed["lexical"]) == 2
    assert len(lexical["hits"]["hits"]) == 2
    assert len(dense["hits"]["hits"]) == 2


@pytest.mark.asyncio
async def test_empty_eligible_set_executes_no_retrieval_lane():
    called = False

    async def execute(_body: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    result = await execute_metadata_restricted_lane(
        {"query": {"match_all": {}}, "size": 50},
        _restriction(()),
        execute=execute,
    )

    assert called is False
    assert result["hits"]["hits"] == []


@pytest.mark.asyncio
async def test_partitioned_lane_merges_only_dls_scoped_counts_and_facets():
    restriction = _restriction(tuple(f"entity-{index:04d}" for index in range(600)))
    responses = iter(
        [
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "chunk-a",
                            "_score": 2.0,
                            "_source": {"chunk_id": "chunk-a"},
                        }
                    ],
                    "total": {"value": 3, "relation": "eq"},
                },
                "aggregations": {
                    "document_types": {
                        "buckets": [{"key": "application/pdf", "doc_count": 3}],
                        "sum_other_doc_count": 0,
                        "doc_count_error_upper_bound": 0,
                    }
                },
            },
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "chunk-b",
                            "_score": 1.0,
                            "_source": {"chunk_id": "chunk-b"},
                        }
                    ],
                    "total": {"value": 2, "relation": "eq"},
                },
                "aggregations": {
                    "document_types": {
                        "buckets": [
                            {"key": "application/pdf", "doc_count": 1},
                            {"key": "text/plain", "doc_count": 1},
                        ],
                        "sum_other_doc_count": 0,
                        "doc_count_error_upper_bound": 0,
                    }
                },
            },
        ]
    )

    async def execute(_body: dict[str, Any]) -> dict[str, Any]:
        return next(responses)

    result = await execute_metadata_restricted_lane(
        {
            "query": {"match_all": {}},
            "size": 10,
            "aggs": {"document_types": {"terms": {"field": "mimetype", "size": 10}}},
        },
        restriction,
        execute=execute,
    )

    assert result["hits"]["total"] == {"value": 5, "relation": "eq"}
    assert result["aggregations"]["document_types"]["buckets"] == [
        {"key": "application/pdf", "doc_count": 4},
        {"key": "text/plain", "doc_count": 1},
    ]
