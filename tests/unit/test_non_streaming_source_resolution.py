from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.search_service as search_service_module
from services.search_service import SearchService


@pytest.mark.asyncio
async def test_resolve_cited_chunks_uses_dls_client_and_preserves_citation_order(monkeypatch):
    client = SimpleNamespace(
        search=AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_id": "os-2",
                            "_source": {
                                "chunk_id": "CHUNK_2",
                                "document_id": "DOCUMENT_2",
                                "filename": "second.pdf",
                                "text": "second evidence",
                                "page": 2,
                                "source_url": "/api/source-files/DOCUMENT_2.token",
                                "chunk_index": 7,
                                "chunking_strategy": "hybrid",
                            },
                        },
                        {
                            "_id": "os-1",
                            "_source": {
                                "chunk_id": "CHUNK_1",
                                "document_id": "DOCUMENT_1",
                                "filename": "first.pdf",
                                "text": "first evidence",
                                "page": 1,
                                "source_url": "/api/source-files/DOCUMENT_1.token",
                                "source_provenance": {
                                    "schema_version": "1.0",
                                    "entity": {
                                        "id": "urn:openrag:document:first",
                                        "type": "document",
                                    },
                                },
                                "source_entity_id": "urn:openrag:document:first",
                                "source_relation_roles": ["contained_in"],
                                "chunk_index": 3,
                                "chunking_strategy": "hybrid",
                            },
                        },
                    ]
                }
            }
        )
    )
    session_manager = SimpleNamespace(get_user_opensearch_client=lambda user_id, jwt_token: client)
    monkeypatch.setattr(search_service_module, "get_index_name", lambda: "documents")
    service = SearchService(session_manager=session_manager)

    sources = await service.resolve_cited_chunks(
        ["CHUNK_1", "CHUNK_2", "CHUNK_1", "MISSING"],
        user_id="user-42",
        jwt_token="jwt-42",
        filters={"data_sources": ["first.pdf", "second.pdf"]},
    )

    assert [source["chunk_id"] for source in sources] == ["CHUNK_1", "CHUNK_2"]
    assert sources[0]["source_url"] == "/api/source-files/DOCUMENT_1.token"
    assert sources[0]["document_id"] == "DOCUMENT_1"
    assert sources[0]["source_entity_id"] == "urn:openrag:document:first"
    assert sources[0]["source_relation_roles"] == ["contained_in"]
    body = client.search.await_args.kwargs["body"]
    assert body["size"] == 3
    assert "source_provenance" in body["_source"]
    assert "source_entity_id" in body["_source"]
    assert body["query"]["bool"]["filter"] == [{"terms": {"filename": ["first.pdf", "second.pdf"]}}]
    assert session_manager.get_user_opensearch_client("user-42", "jwt-42") is client
