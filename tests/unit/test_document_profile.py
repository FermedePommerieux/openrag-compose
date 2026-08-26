"""Deterministic ingestion-profile contracts for verifiable retrieval."""

import hashlib
from typing import Any

import pytest

from services.document_index_writer import DocumentIndexContext, DocumentIndexWriter


class ProfileOpenSearch:
    def __init__(self) -> None:
        self.update_body: dict[str, Any] | None = None

    async def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("size") == 0:
            return {"hits": {"total": {"value": 2, "relation": "eq"}}}
        first_text = "a" * 20
        second_text = "b" * 22
        return {
            "hits": {
                "hits": [
                    {
                        "sort": [0, "chunk-a"],
                        "_source": {
                            "chunk_id": "chunk-a",
                            "chunk_content_sha256": hashlib.sha256(first_text.encode()).hexdigest(),
                            "chunk_character_count": len(first_text),
                            "chunk_index": 0,
                            "page": 1,
                            "text": first_text,
                        },
                    },
                    {
                        "sort": [1, "chunk-b"],
                        "_source": {
                            "chunk_id": "chunk-b",
                            "chunk_content_sha256": hashlib.sha256(
                                second_text.encode()
                            ).hexdigest(),
                            "chunk_character_count": len(second_text),
                            "chunk_index": 1,
                            "page": 9,
                            "text": second_text,
                        },
                    },
                ]
            }
        }

    async def update_by_query(self, *, index: str, body: dict[str, Any], **kwargs):
        self.update_body = body
        return {"updated": 2}


@pytest.mark.asyncio
async def test_profile_covers_all_chunks_and_stamps_snapshot_digest():
    client = ProfileOpenSearch()
    writer = DocumentIndexWriter(opensearch_client=client)
    context = DocumentIndexContext(
        document_id="document-1",
        owner="user-1",
        ingest_run_id="run-1",
        mimetype="application/pdf",
        embedding_model="text-embedding-3-large",
    )

    profile = await writer._finalize_document_profile(context, index_name="documents")

    assert profile["document_chunk_count"] == 2
    assert profile["document_page_count"] == 2
    assert profile["document_max_page"] == 9
    assert profile["document_character_count"] == 42
    assert profile["document_size_class"] == "small"
    assert len(profile["document_content_sha256"]) == 64
    assert profile["document_order_verified"] is True
    assert client.update_body is not None
    assert (
        client.update_body["script"]["params"]["content_digest"]
        == profile["document_content_sha256"]
    )
    filters = client.update_body["query"]["bool"]["filter"]
    assert {"term": {"document_id": "document-1"}} in filters
    assert {"term": {"ingest_run_id": "run-1"}} in filters
    assert {"term": {"owner": "user-1"}} in filters


@pytest.mark.asyncio
async def test_profile_refuses_incomplete_snapshot_hash():
    client = ProfileOpenSearch()
    original_search = client.search

    async def incomplete_search(*, index: str, body: dict[str, Any]):
        response = await original_search(index=index, body=body)
        if body.get("size") != 0:
            response["hits"]["hits"] = response["hits"]["hits"][:1]
        return response

    client.search = incomplete_search
    writer = DocumentIndexWriter(opensearch_client=client)
    context = DocumentIndexContext(
        document_id="document-1",
        owner="user-1",
        ingest_run_id="run-1",
        mimetype="application/pdf",
        embedding_model="text-embedding-3-large",
    )

    with pytest.raises(RuntimeError, match="snapshot is incomplete"):
        await writer._finalize_document_profile(context, index_name="documents")


@pytest.mark.asyncio
async def test_profile_refuses_duplicate_or_gapped_chunk_order():
    client = ProfileOpenSearch()
    original_search = client.search

    async def invalid_order_search(*, index: str, body: dict[str, Any]):
        response = await original_search(index=index, body=body)
        if body.get("size") != 0:
            response["hits"]["hits"][1]["_source"]["chunk_index"] = 9
        return response

    client.search = invalid_order_search
    writer = DocumentIndexWriter(opensearch_client=client)
    context = DocumentIndexContext(
        document_id="document-1",
        owner="user-1",
        ingest_run_id="run-1",
        mimetype="application/pdf",
        embedding_model="text-embedding-3-large",
    )

    with pytest.raises(RuntimeError, match="unique contiguous chunk_index"):
        await writer._finalize_document_profile(context, index_name="documents")


@pytest.mark.asyncio
async def test_profile_recalculates_chunk_text_digest():
    client = ProfileOpenSearch()
    original_search = client.search

    async def corrupted_text_search(*, index: str, body: dict[str, Any]):
        response = await original_search(index=index, body=body)
        if body.get("size") != 0:
            response["hits"]["hits"][0]["_source"]["text"] = "changed after hashing"
        return response

    client.search = corrupted_text_search
    writer = DocumentIndexWriter(opensearch_client=client)
    context = DocumentIndexContext(
        document_id="document-1",
        owner="user-1",
        ingest_run_id="run-1",
        mimetype="application/pdf",
        embedding_model="text-embedding-3-large",
    )

    with pytest.raises(RuntimeError, match="text digest mismatch"):
        await writer._finalize_document_profile(context, index_name="documents")
