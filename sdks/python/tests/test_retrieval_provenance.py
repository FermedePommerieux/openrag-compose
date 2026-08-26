"""Retrieval v2 provenance must survive every Python SDK parsing path."""

import json

import httpx
import pytest

from openrag_sdk import EvidenceCoverage, OpenRAGClient, SearchResult, Source

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
    "chunk_content_sha256": "a" * 64,
    "document_content_sha256": "b" * 64,
    "evidence_order": 7,
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
async def test_v1_exhaustive_search_sends_cursor_and_parses_coverage():
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        cursor = body.get("cursor")
        covered = 2 if cursor else 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        **PROVENANCE,
                        "score": None,
                        "evidence_order": covered - 1,
                    }
                ],
                "coverage": {
                    "mode": "exhaustive",
                    "document_id": "document-123",
                    "snapshot_sha256": "b" * 64,
                    "covered_chunks": covered,
                    "total_chunks": 2,
                    "coverage_ratio": covered / 2,
                    "complete": covered == 2,
                    "next_cursor": None if covered == 2 else "opaque-cursor",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRAGClient(
            api_key="test-key", base_url="https://openrag.test", http_client=http_client
        )
        first = await client.search.query(
            "",
            evidence_mode="exhaustive",
            document_id="document-123",
            batch_size=1,
        )
        second = await client.search.query(
            "",
            evidence_mode="exhaustive",
            document_id="document-123",
            cursor=first.coverage.next_cursor,
            batch_size=1,
        )

    assert first.coverage == EvidenceCoverage(
        document_id="document-123",
        snapshot_sha256="b" * 64,
        covered_chunks=1,
        total_chunks=2,
        coverage_ratio=0.5,
        complete=False,
        next_cursor="opaque-cursor",
    )
    assert second.coverage is not None and second.coverage.complete is True
    assert first.results[0].score is None
    assert requests[0]["evidence_mode"] == "exhaustive"
    assert requests[0]["document_id"] == "document-123"
    assert requests[0]["batch_size"] == 1
    assert requests[1]["cursor"] == "opaque-cursor"


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
            event async for event in await client.chat.create("report", stream=True)
        ]

    assert events[0].sources[0].model_dump() == PROVENANCE
