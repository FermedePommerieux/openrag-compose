"""Retrieval v2 provenance must survive every Python SDK parsing path."""

import json

import httpx
import pytest

from openrag_sdk import OpenRAGClient, SearchResult, Source

PROVENANCE = {
    "filename": "report.pdf",
    "text": "retrieved evidence",
    "score": 0.98,
    "page": 4,
    "mimetype": "application/pdf",
    "source_url": "https://example.test/report.pdf",
    "document_id": "document-123",
    "chunk_id": "document-123:chunk-7",
    "connector_file_id": "drive-file-456",
    "chunk_index": 7,
    "chunking_strategy": "hybrid",
}


def test_source_model_preserves_complete_retrieval_provenance():
    assert Source(**PROVENANCE).model_dump() == PROVENANCE


def test_source_model_remains_compatible_with_legacy_and_source_url_only_responses():
    legacy = Source(filename="legacy.txt", text="old", score=0.1)
    assert legacy.document_id is None
    assert legacy.chunk_id is None

    source_url_only = Source(
        filename="linked.txt",
        text="linked",
        score=0.2,
        source_url="https://example.test/linked.txt",
    )
    assert source_url_only.source_url == "https://example.test/linked.txt"
    assert source_url_only.connector_file_id is None


def test_search_result_model_preserves_complete_retrieval_provenance():
    assert SearchResult(**PROVENANCE).model_dump() == PROVENANCE


@pytest.mark.asyncio
async def test_v1_search_parsing_preserves_complete_retrieval_provenance():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search"
        return httpx.Response(200, json={"results": [PROVENANCE]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRAGClient(
            api_key="test-key", base_url="https://openrag.test", http_client=http_client
        )
        response = await client.search.query("report")

    assert response.results[0].model_dump() == PROVENANCE


@pytest.mark.asyncio
async def test_chat_sse_parsing_preserves_complete_retrieval_provenance():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat"
        payload = (
            f"data: {json.dumps({'type': 'sources', 'sources': [PROVENANCE]})}\n\n"
            'data: {"type": "done", "chat_id": "chat-123"}\n\n'
        )
        return httpx.Response(200, content=payload.encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRAGClient(
            api_key="test-key", base_url="https://openrag.test", http_client=http_client
        )
        events = [
            event
            async for event in await client.chat.create("report", stream=True)
        ]

    assert events[0].sources[0].model_dump() == PROVENANCE
