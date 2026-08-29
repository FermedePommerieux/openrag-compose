from unittest.mock import AsyncMock, Mock

import pytest

from src.api.files import list_files
from src.services.file_service import FileService


@pytest.mark.asyncio
async def test_list_files_filters_exact_data_sources_in_one_query() -> None:
    client = AsyncMock()
    client.search.return_value = {
        "hits": {
            "total": {"value": 28_313, "relation": "eq"},
            "hits": [
                {
                    "_source": {
                        "document_id": "document-1",
                        "filename": "invoice.pdf",
                        "document_chunk_count": 9,
                    },
                    "sort": ["invoice.pdf", "document-1"],
                }
            ],
        },
    }
    session_manager = Mock()
    session_manager.get_user_opensearch_client.return_value = client

    service = FileService(session_manager=session_manager)
    result = await service.list_files(
        user_id="connector-user",
        data_sources=["mail.eml", "invoice.pdf", "mail.eml"],
        page_size=3,
    )

    assert result["total"] == 28_313
    assert result["files"][0]["document_id"] == "document-1"
    assert result["files"][0]["chunk_count"] == 9
    assert result["next_cursor"] is not None
    body = client.search.await_args.kwargs["body"]
    assert body["size"] == 18
    assert body["track_total_hits"] is True
    assert "aggs" not in body
    query = body["query"]
    assert query["bool"]["filter"] == [
        {"terms": {"filename": ["mail.eml", "invoice.pdf"]}},
        {"term": {"chunk_index": 0}},
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
        "next_cursor": None,
        "prefetched_pages": [],
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
        cursor="opaque-cursor",
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
        cursor="opaque-cursor",
    )


@pytest.mark.asyncio
async def test_list_files_uses_search_after_cursor_beyond_result_window() -> None:
    client = AsyncMock()
    client.search.return_value = {
        "hits": {"total": {"value": 28_313, "relation": "eq"}, "hits": []}
    }
    session_manager = Mock()
    session_manager.get_user_opensearch_client.return_value = client
    service = FileService(session_manager=session_manager)
    cursor = service._encode_cursor(["page-boundary.pdf", "document-10000"])

    result = await service.list_files(
        user_id="connector-user",
        page=201,
        page_size=50,
        cursor=cursor,
    )

    body = client.search.await_args.kwargs["body"]
    assert body["size"] == 300
    assert body["search_after"] == ["page-boundary.pdf", "document-10000"]
    assert "from" not in body
    assert result["total"] == 28_313


@pytest.mark.asyncio
async def test_list_files_keeps_equal_filenames_as_distinct_documents() -> None:
    client = AsyncMock()
    client.search.return_value = {
        "hits": {
            "total": {"value": 2, "relation": "eq"},
            "hits": [
                {
                    "_source": {"document_id": "document-a", "filename": "same.pdf"},
                    "sort": ["same.pdf", "document-a"],
                },
                {
                    "_source": {"document_id": "document-b", "filename": "same.pdf"},
                    "sort": ["same.pdf", "document-b"],
                },
            ],
        }
    }
    session_manager = Mock()
    session_manager.get_user_opensearch_client.return_value = client

    result = await FileService(session_manager=session_manager).list_files(
        user_id="connector-user",
        page_size=50,
    )

    assert [file["document_id"] for file in result["files"]] == [
        "document-a",
        "document-b",
    ]
    assert result["total"] == 2


@pytest.mark.asyncio
async def test_list_files_rejects_invalid_cursor_before_query() -> None:
    client = AsyncMock()
    session_manager = Mock()
    session_manager.get_user_opensearch_client.return_value = client

    with pytest.raises(ValueError, match="Invalid file pagination cursor"):
        await FileService(session_manager=session_manager).list_files(
            user_id="connector-user",
            page=2,
            cursor="not-base64-json",
        )

    client.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_files_prefetches_exactly_five_following_pages() -> None:
    hits = [
        {
            "_source": {
                "document_id": f"document-{index}",
                "filename": f"file-{index}.pdf",
            },
            "sort": [f"file-{index}.pdf", f"document-{index}"],
        }
        for index in range(14)
    ]
    client = AsyncMock()
    client.search.return_value = {
        "hits": {"total": {"value": 14, "relation": "eq"}, "hits": hits[:12]}
    }
    session_manager = Mock()
    session_manager.get_user_opensearch_client.return_value = client

    result = await FileService(session_manager=session_manager).list_files(
        user_id="connector-user",
        page_size=2,
    )

    assert client.search.await_args.kwargs["body"]["size"] == 12
    assert [file["document_id"] for file in result["files"]] == [
        "document-0",
        "document-1",
    ]
    assert [page["page"] for page in result["prefetched_pages"]] == [2, 3, 4, 5, 6]
    assert [file["document_id"] for file in result["prefetched_pages"][0]["files"]] == [
        "document-2",
        "document-3",
    ]
    assert result["prefetched_pages"][-1]["next_cursor"] is not None
