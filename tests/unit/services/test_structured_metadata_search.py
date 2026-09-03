from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.search import SearchV1Body
from models.document_investigation import CalendarBasis
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterClause,
    MetadataFilterField,
    MetadataFilterOperator,
)
from models.structured_document_query import (
    MetadataCandidateDiagnostics,
    MetadataCandidateRestriction,
)
from services.search_service import SearchService


def _filter() -> MetadataFilter:
    return MetadataFilter(
        clauses=(
            MetadataFilterClause(
                field=MetadataFilterField.PRODUCTION_MONTH,
                operator=MetadataFilterOperator.EQUAL,
                values=("2024-03",),
                calendar_basis=CalendarBasis.SOURCE_LOCAL,
                source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
            ),
        )
    )


def _restriction(metadata_filter: MetadataFilter) -> MetadataCandidateRestriction:
    return MetadataCandidateRestriction(
        source_entity_ids=("visible-occurrence",),
        projection_alias="documents-metadata-filter-current",
        diagnostics=MetadataCandidateDiagnostics(
            filter_sha256=metadata_filter.calculate_sha256(),
            filters_requested=1,
            filters_effective=1,
            visible_projection_count=3,
            eligible_count=1,
            pages=1,
        ),
    )


def test_v1_schema_keeps_explicit_free_text_and_metadata_filter_separate():
    body = SearchV1Body(free_text="factures Orange", metadata_filter=_filter())

    assert body.resolved_free_text == "factures Orange"
    assert body.metadata_filter == _filter()

    with pytest.raises(ValueError, match="cannot disagree"):
        SearchV1Body(query="one", free_text="two", metadata_filter=_filter())

    with pytest.raises(ValueError, match="free_text is required"):
        SearchV1Body(metadata_filter=_filter())


@pytest.mark.asyncio
async def test_search_resolves_metadata_with_dls_client_and_passes_exact_restriction(monkeypatch):
    metadata_filter = _filter()
    restriction = _restriction(metadata_filter)
    dls_client = object()
    service = SearchService.__new__(SearchService)
    service.session_manager = MagicMock()
    service.session_manager.get_user_opensearch_client.return_value = dls_client
    service.search_tool = AsyncMock(return_value={"results": []})
    resolver = AsyncMock(return_value=restriction)
    monkeypatch.setattr("services.search_service.resolve_metadata_candidates", resolver)

    result = await service.search(
        "factures Orange",
        user_id="user-1",
        jwt_token="jwt-1",
        metadata_filter=metadata_filter,
    )

    assert result == {"results": []}
    resolver.assert_awaited_once_with(dls_client, metadata_filter)
    service.search_tool.assert_awaited_once_with(
        "factures Orange",
        embedding_model=None,
        group_by_document=False,
        page=1,
        page_size=100,
        _metadata_restriction=restriction,
    )


@pytest.mark.asyncio
async def test_direct_exhaustive_read_rejects_metadata_filter():
    service = SearchService.__new__(SearchService)

    with pytest.raises(ValueError, match="not supported"):
        await service.search(
            "",
            user_id="user-1",
            jwt_token="jwt-1",
            evidence_mode="exhaustive",
            document_id="document-1",
            metadata_filter=_filter(),
        )


@pytest.mark.asyncio
async def test_metadata_filter_without_free_text_fails_explicitly():
    service = SearchService.__new__(SearchService)

    with pytest.raises(ValueError, match="free_text is required"):
        await service.search(
            "",
            user_id="user-1",
            jwt_token="jwt-1",
            metadata_filter=_filter(),
        )
