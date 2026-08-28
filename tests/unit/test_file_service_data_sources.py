from unittest.mock import AsyncMock, Mock

import pytest

from src.api.files import list_files
from src.services.file_service import FileService


@pytest.mark.asyncio
async def test_list_files_filters_exact_data_sources_in_one_query() -> None:
    client = AsyncMock()
    client.search.return_value = {
        "aggregations": {"files": {"buckets": []}},
    }
    session_manager = Mock()
    session_manager.get_user_opensearch_client.return_value = client

    service = FileService(session_manager=session_manager)
    result = await service.list_files(
        user_id="connector-user",
        data_sources=["mail.eml", "invoice.pdf", "mail.eml"],
        page_size=3,
    )

    assert result["files"] == []
    query = client.search.await_args.kwargs["body"]["query"]
    assert query["bool"]["filter"] == [
        {"terms": {"filename": ["mail.eml", "invoice.pdf"]}},
    ]


def test_empty_data_sources_keeps_unfiltered_listing() -> None:
    query = FileService()._build_filter_query(
        "connector-user",
        data_sources=[],
    )

    assert query == {"bool": {"filter": []}}


@pytest.mark.asyncio
async def test_files_api_forwards_exact_data_sources() -> None:
    service = AsyncMock()
    service.list_files.return_value = {
        "files": [],
        "total": 0,
        "page": 1,
        "page_size": 2,
    }
    user = Mock(user_id="connector-user", jwt_token="scoped-token")

    response = await list_files(
        page=1,
        page_size=2,
        sort_by="filename",
        sort_order="asc",
        connector_type=None,
        mimetype=None,
        owner=None,
        search=None,
        data_sources=["mail.eml", "invoice.pdf"],
        file_service=service,
        user=user,
    )

    assert response.status_code == 200
    service.list_files.assert_awaited_once_with(
        user_id="connector-user",
        jwt_token="scoped-token",
        page=1,
        page_size=2,
        sort_by="filename",
        sort_order="asc",
        connector_type=None,
        mimetype=None,
        owner=None,
        search=None,
        data_sources=["mail.eml", "invoice.pdf"],
    )
