"""DLS-first metadata candidate restriction for lexical and dense lanes.

This is a phase-1 internal primitive.  It does not create the side index and
does not alter ranking.  A caller must provide the same user-scoped OpenSearch
client for side-index resolution and retrieval defense in depth.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from models.metadata_filter import (
    MetadataFilter,
    MetadataFilterBooleanOperator,
    MetadataFilterExpression,
    MetadataFilterField,
    MetadataFilterOperator,
)
from models.structured_document_query import (
    MetadataCandidateDiagnostics,
    MetadataCandidateRestriction,
)
from services.metadata_filter_projection import (
    MetadataProjectionQueryBoundary,
    compile_metadata_filter_to_opensearch,
)

METADATA_FILTER_PROJECTION_ALIAS = "documents-metadata-filter-current"
METADATA_CANDIDATE_PAGE_SIZE = 512
METADATA_RETRIEVAL_TERMS_BATCH = 512
MAX_METADATA_FILTER_PREDICATES = 32
MAX_METADATA_FILTER_DEPTH = 4
MAX_METADATA_FILTER_IN_VALUES = 64
MAX_METADATA_FILTER_OR_BRANCHES = 16
MAX_METADATA_ELIGIBLE_OCCURRENCES = 50_000

SUPPORTED_METADATA_FILTER_FIELDS = frozenset(
    {
        MetadataFilterField.PRODUCTION_DAY,
        MetadataFilterField.PRODUCTION_MONTH,
        MetadataFilterField.PRODUCTION_YEAR,
        MetadataFilterField.MODIFICATION_DAY,
        MetadataFilterField.MODIFICATION_MONTH,
        MetadataFilterField.MODIFICATION_YEAR,
        MetadataFilterField.MIME,
        MetadataFilterField.FORMAT_FAMILY,
        MetadataFilterField.EXTENSION,
        MetadataFilterField.SOURCE_DOCUMENT_TYPE,
        MetadataFilterField.SOURCE_SYSTEM,
        MetadataFilterField.SOURCE_ENTITY_TYPE,
        MetadataFilterField.SOURCE_ENTITY_FAMILY,
        MetadataFilterField.PARENT_COLLECTION,
        MetadataFilterField.CONNECTOR,
        MetadataFilterField.CREATOR_OBSERVATION,
        MetadataFilterField.LAST_MODIFIER_OBSERVATION,
        MetadataFilterField.PRODUCER_OBSERVATION,
        MetadataFilterField.CREATOR_APPLICATION_OBSERVATION,
        MetadataFilterField.BINARY_SHA256,
        MetadataFilterField.HAS_TEMPORAL_CONFLICT,
        MetadataFilterField.HAS_METADATA_CONFLICT,
    }
)


def _expression_metrics(expression: MetadataFilterExpression, *, depth: int = 1) -> tuple[int, int]:
    if expression.clause is not None:
        return 1, depth
    if (
        expression.operator == MetadataFilterBooleanOperator.OR
        and len(expression.children) > MAX_METADATA_FILTER_OR_BRANCHES
    ):
        raise ValueError(
            f"metadata filter OR supports at most {MAX_METADATA_FILTER_OR_BRANCHES} branches"
        )
    child_metrics = [_expression_metrics(child, depth=depth + 1) for child in expression.children]
    return sum(item[0] for item in child_metrics), max(item[1] for item in child_metrics)


def _clauses(metadata_filter: MetadataFilter) -> tuple[Any, ...]:
    if metadata_filter.expression is None:
        return metadata_filter.clauses

    found: list[Any] = []

    def visit(expression: MetadataFilterExpression) -> None:
        if expression.clause is not None:
            found.append(expression.clause)
            return
        for child in expression.children:
            visit(child)

    visit(metadata_filter.expression)
    return tuple(found)


def validate_metadata_filter_complexity(metadata_filter: MetadataFilter) -> int:
    """Validate public-safe fields and hard query-construction bounds."""
    clauses = _clauses(metadata_filter)
    if metadata_filter.expression is not None:
        predicate_count, depth = _expression_metrics(metadata_filter.expression)
    else:
        predicate_count, depth = len(clauses), 1
    if predicate_count > MAX_METADATA_FILTER_PREDICATES:
        raise ValueError(
            f"metadata filter supports at most {MAX_METADATA_FILTER_PREDICATES} predicates"
        )
    if depth > MAX_METADATA_FILTER_DEPTH:
        raise ValueError(f"metadata filter nesting depth exceeds {MAX_METADATA_FILTER_DEPTH}")
    unsupported = sorted(
        {
            clause.field.value
            for clause in clauses
            if clause.field not in SUPPORTED_METADATA_FILTER_FIELDS
        }
    )
    if unsupported:
        raise ValueError(f"unsupported_filter: {','.join(unsupported)}")
    for clause in clauses:
        if (
            clause.operator == MetadataFilterOperator.IN
            and len(clause.values) > MAX_METADATA_FILTER_IN_VALUES
        ):
            raise ValueError(
                f"metadata filter IN supports at most {MAX_METADATA_FILTER_IN_VALUES} values"
            )
    return predicate_count


async def resolve_metadata_candidates(
    client: Any,
    metadata_filter: MetadataFilter,
    *,
    projection_alias: str = METADATA_FILTER_PROJECTION_ALIAS,
    page_size: int = METADATA_CANDIDATE_PAGE_SIZE,
) -> MetadataCandidateRestriction:
    """Resolve only TRUE rows through a DLS-scoped side-index client."""
    predicate_count = validate_metadata_filter_complexity(metadata_filter)
    bounded_page = min(METADATA_CANDIDATE_PAGE_SIZE, max(1, int(page_size)))
    query = compile_metadata_filter_to_opensearch(
        metadata_filter,
        boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
    )
    visible_projection_count = int((await client.count(index=projection_alias))["count"])
    ids: set[str] = set()
    search_after: list[Any] | None = None
    pages = 0
    while True:
        body: dict[str, Any] = {
            "query": query,
            "_source": ["source_entity_id"],
            "size": bounded_page,
            "track_total_hits": True,
            "sort": [
                {"source_entity_id": {"order": "asc"}},
                {"source_document_id": {"order": "asc"}},
                {"projection_document_id": {"order": "asc"}},
            ],
        }
        if search_after is not None:
            body["search_after"] = search_after
        response = await client.search(index=projection_alias, body=body)
        pages += 1
        hits = response.get("hits", {}).get("hits", [])
        for hit in hits:
            entity_id = str((hit.get("_source") or {}).get("source_entity_id") or "").strip()
            if not entity_id:
                raise RuntimeError("metadata projection row has no source_entity_id")
            ids.add(entity_id)
        if len(ids) > MAX_METADATA_ELIGIBLE_OCCURRENCES:
            raise ValueError("metadata filter eligible set exceeds the bounded transfer contract")
        if len(hits) < bounded_page:
            break
        cursor = hits[-1].get("sort")
        if not isinstance(cursor, list):
            raise RuntimeError("metadata candidate pagination has no stable cursor")
        search_after = cursor
    ordered = tuple(sorted(ids))
    return MetadataCandidateRestriction(
        source_entity_ids=ordered,
        projection_alias=projection_alias,
        diagnostics=MetadataCandidateDiagnostics(
            filter_sha256=metadata_filter.calculate_sha256(),
            filters_requested=predicate_count,
            filters_effective=predicate_count,
            visible_projection_count=visible_projection_count,
            eligible_count=len(ordered),
            pages=pages,
        ),
    )


def candidate_id_partitions(
    candidate_ids: Iterable[str],
    *,
    batch_size: int = METADATA_RETRIEVAL_TERMS_BATCH,
) -> tuple[tuple[str, ...], ...]:
    ordered = tuple(sorted(set(candidate_ids)))
    bounded = min(METADATA_RETRIEVAL_TERMS_BATCH, max(1, int(batch_size)))
    return tuple(ordered[offset : offset + bounded] for offset in range(0, len(ordered), bounded))


def restrict_lane_body(body: dict[str, Any], candidate_ids: Iterable[str]) -> dict[str, Any]:
    """Inject the identical occurrence restriction into one ranking lane."""
    ids = tuple(sorted(set(candidate_ids)))
    if not ids:
        raise ValueError("a retrieval partition cannot be empty")
    if len(ids) > METADATA_RETRIEVAL_TERMS_BATCH:
        raise ValueError("a retrieval partition exceeds the terms bound")
    restricted = copy.deepcopy(body)
    query = restricted.get("query")
    if not isinstance(query, dict):
        raise ValueError("retrieval lane has no query")
    bool_query = query.get("bool")
    if not isinstance(bool_query, dict):
        restricted["query"] = {
            "bool": {
                "must": [query],
                "filter": [{"terms": {"source_entity_id": list(ids)}}],
            }
        }
        return restricted
    filters = bool_query.setdefault("filter", [])
    if isinstance(filters, dict):
        filters = [filters]
        bool_query["filter"] = filters
    if not isinstance(filters, list):
        raise ValueError("retrieval lane bool.filter must be an object or array")
    filters.append({"terms": {"source_entity_id": list(ids)}})
    return restricted


def _merge_lane_responses(responses: list[dict[str, Any]], *, size: int) -> dict[str, Any]:
    hits = [hit for response in responses for hit in response.get("hits", {}).get("hits", [])]
    unique: dict[str, dict[str, Any]] = {}
    for hit in hits:
        identity = str(hit.get("_id") or (hit.get("_source") or {}).get("chunk_id") or "")
        if not identity:
            raise RuntimeError("metadata-restricted lane returned a hit without identity")
        previous = unique.get(identity)
        if previous is None or float(hit.get("_score") or 0.0) > float(
            previous.get("_score") or 0.0
        ):
            unique[identity] = hit
    ordered = sorted(
        unique.values(),
        key=lambda hit: (
            -float(hit.get("_score") or 0.0),
            str((hit.get("_source") or {}).get("chunk_id") or hit.get("_id") or ""),
        ),
    )[:size]
    return {
        "hits": {
            "hits": ordered,
            "total": {"value": len(unique), "relation": "eq"},
        }
    }


async def execute_metadata_restricted_lane(
    body: dict[str, Any],
    restriction: MetadataCandidateRestriction,
    *,
    execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Execute bounded ID partitions and preserve the lane's original scores."""
    size = max(0, int(body.get("size", 10)))
    partitions = candidate_id_partitions(restriction.source_entity_ids)
    if not partitions:
        return {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}}
    responses = [await execute(restrict_lane_body(body, partition)) for partition in partitions]
    return _merge_lane_responses(responses, size=size)
