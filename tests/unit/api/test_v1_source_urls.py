"""Public v1 APIs must preserve source URLs returned by retrieval."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.chat import _extract_sources
from api.v1.search import SearchV1Body, search_endpoint
from session_manager import User

SOURCE_URL = "https://files.example.com/report.pdf"


def test_chat_source_extraction_preserves_complete_provenance():
    """Streaming chat keeps all Retrieval v2 provenance fields."""
    sources = _extract_sources(
        {
            "results": [
                {
                    "filename": "report.pdf",
                    "text": "Evidence",
                    "score": 0.91,
                    "page": 3,
                    "mimetype": "application/pdf",
                    "source_url": SOURCE_URL,
                    "document_id": "document-1",
                    "chunk_id": "chunk-7",
                    "connector_file_id": "drive-file-1",
                    "chunk_index": 7,
                    "chunking_strategy": "hybrid",
                }
            ]
        }
    )

    assert sources == [
        {
            "filename": "report.pdf",
            "text": "Evidence",
            "score": 0.91,
            "page": 3,
            "mimetype": "application/pdf",
            "source_url": SOURCE_URL,
            "document_id": "document-1",
            "chunk_id": "chunk-7",
            "connector_file_id": "drive-file-1",
            "chunk_index": 7,
            "chunking_strategy": "hybrid",
        }
    ]


def test_chat_source_extraction_keeps_legacy_provenance_optional():
    """Legacy results retain their available fields without fabricated IDs."""
    sources = _extract_sources(
        {"results": [{"text": "Legacy evidence", "filename": "legacy.txt", "document_id": "old"}]}
    )

    assert sources == [
        {
            "filename": "legacy.txt",
            "text": "Legacy evidence",
            "score": 0,
            "page": None,
            "mimetype": None,
            "source_url": None,
            "document_id": "old",
        }
    ]


def test_chat_source_extraction_preserves_source_url_without_other_provenance():
    sources = _extract_sources({"results": [{"text": "Evidence", "source_url": SOURCE_URL}]})

    assert sources == [
        {
            "filename": "",
            "text": "Evidence",
            "score": 0,
            "page": None,
            "mimetype": None,
            "source_url": SOURCE_URL,
        }
    ]


def test_chat_source_extraction_accepts_results_with_absent_optional_fields():
    assert _extract_sources({"results": [{"text": "Evidence"}]}) == [
        {
            "filename": "",
            "text": "Evidence",
            "score": 0,
            "page": None,
            "mimetype": None,
            "source_url": None,
        }
    ]


@pytest.mark.asyncio
async def test_search_endpoint_preserves_source_url():
    """Preserve source URLs in public search responses."""
    search_service = MagicMock()
    search_service.search = AsyncMock(
        return_value={
            "results": [
                {
                    "filename": "report.pdf",
                    "text": "Evidence",
                    "score": 0.91,
                    "page": 3,
                    "mimetype": "application/pdf",
                    "source_url": SOURCE_URL,
                    "document_id": "document-1",
                    "chunk_id": "chunk-7",
                    "chunk_index": 7,
                    "chunking_strategy": "hybrid",
                    "connector_file_id": "drive-file-1",
                }
            ]
        }
    )
    user = User(
        user_id="user-1",
        email="u@example.com",
        name="User",
        jwt_token="Bearer tok",
    )

    response = await search_endpoint(
        SearchV1Body(query="evidence"),
        search_service=search_service,
        user=user,
        knowledge_filter_service=MagicMock(),
    )

    payload = json.loads(response.body.decode())
    assert payload["results"][0]["source_url"] == SOURCE_URL
    assert payload["results"][0]["document_id"] == "document-1"
    assert payload["results"][0]["chunk_id"] == "chunk-7"
    assert payload["results"][0]["chunk_index"] == 7
    assert payload["results"][0]["chunking_strategy"] == "hybrid"
    assert payload["results"][0]["connector_file_id"] == "drive-file-1"


@pytest.mark.asyncio
async def test_search_endpoint_preserves_exhaustive_coverage_contract():
    search_service = MagicMock()
    search_service.search = AsyncMock(
        return_value={
            "results": [{"chunk_id": "chunk-1", "text": "Evidence"}],
            "coverage": {
                "mode": "exhaustive",
                "document_id": "document-1",
                "covered_chunks": 20,
                "total_chunks": 40,
                "complete": False,
                "next_cursor": "cursor-2",
            },
        }
    )
    user = User(
        user_id="user-1",
        email="u@example.com",
        name="User",
        jwt_token="Bearer tok",
    )

    response = await search_endpoint(
        SearchV1Body(
            query="audit",
            evidence_mode="exhaustive",
            document_id="document-1",
            cursor="cursor-1",
            batch_size=20,
        ),
        search_service=search_service,
        user=user,
        knowledge_filter_service=MagicMock(),
    )

    payload = json.loads(response.body.decode())
    assert payload["coverage"]["complete"] is False
    search_service.search.assert_awaited_once()
    assert search_service.search.await_args.kwargs["evidence_mode"] == "exhaustive"
    assert search_service.search.await_args.kwargs["document_id"] == "document-1"
    assert search_service.search.await_args.kwargs["cursor"] == "cursor-1"


@pytest.mark.asyncio
async def test_exhaustive_search_accepts_an_empty_ranked_query():
    """Source-order reading is keyed by document id, not a semantic query."""
    search_service = MagicMock()
    search_service.search = AsyncMock(
        return_value={
            "results": [],
            "coverage": {
                "mode": "exhaustive",
                "document_id": "document-1",
                "snapshot_sha256": "a" * 64,
                "covered_chunks": 0,
                "total_chunks": 0,
                "coverage_ratio": 1.0,
                "complete": True,
                "next_cursor": None,
            },
        }
    )
    user = User(
        user_id="user-1",
        email="u@example.com",
        name="User",
        jwt_token="Bearer tok",
    )

    response = await search_endpoint(
        SearchV1Body(
            query="",
            evidence_mode="exhaustive",
            document_id="document-1",
        ),
        search_service=search_service,
        user=user,
        knowledge_filter_service=MagicMock(),
    )

    assert response.status_code == 200
    search_service.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_scope_exhaustive_search_preserves_graph_and_coverage_contract():
    result = {
        "results": [{"chunk_id": "chunk-1", "text": "Evidence"}],
        "documents": [{"document_id": "document-1", "filename": "mail.eml"}],
        "graph": {
            "entities": ["mail-a", "mail-b"],
            "edges": [
                {
                    "source_entity_id": "mail-a",
                    "role": "reply_to",
                    "target_entity_id": "mail-b",
                }
            ],
        },
        "coverage": {
            "mode": "scope_exhaustive",
            "documents_discovered": 1,
            "documents_complete": 1,
            "complete": True,
        },
    }
    search_service = MagicMock()
    search_service.search = AsyncMock(return_value=result)
    user = User(
        user_id="user-1",
        email="u@example.com",
        name="User",
        jwt_token="Bearer tok",
    )

    response = await search_endpoint(
        SearchV1Body(
            query="all exchanges about Surface pastorale",
            evidence_mode="scope_exhaustive",
        ),
        search_service=search_service,
        user=user,
        knowledge_filter_service=MagicMock(),
    )

    assert json.loads(response.body.decode()) == result
    assert search_service.search.await_args.kwargs["evidence_mode"] == "scope_exhaustive"
