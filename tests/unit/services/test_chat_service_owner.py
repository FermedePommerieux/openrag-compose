import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auth_context import set_score_threshold, set_search_filters, set_search_limit  # noqa: E402
from services.chat_service import ChatService  # noqa: E402
from services.document_index_writer import DocumentIndexContext  # noqa: E402


@pytest.mark.asyncio
async def test_langflow_chat_passes_owner_metadata(monkeypatch):
    # Mock settings / clients dependencies
    fake_langflow_client = MagicMock()
    monkeypatch.setattr(
        "config.settings.clients.ensure_langflow_client",
        AsyncMock(return_value=fake_langflow_client),
    )
    monkeypatch.setattr(
        "utils.langflow_headers.add_provider_credentials_to_headers",
        AsyncMock(),
    )

    # Mock async_langflow_chat to prevent actual network/langflow calls
    mock_langflow_chat = AsyncMock(return_value=("some response", "response-id", [], None))
    monkeypatch.setattr("agent.async_langflow_chat", mock_langflow_chat)

    # Capture the context passed to LangflowIngestTokenService.create_token
    captured_context = []

    def mock_create_token(self, context):
        captured_context.append(context)
        return "fake-ingest-token"

    monkeypatch.setattr(
        "services.langflow_ingest_token_service.LangflowIngestTokenService.create_token",
        mock_create_token,
    )

    # Instantiate ChatService and invoke langflow_chat with specific owner metadata
    chat_svc = ChatService()
    set_search_filters({"data_sources": ["archive.pdf"]})
    set_search_limit(4)
    set_score_threshold(0.25)
    await chat_svc.langflow_chat(
        prompt="hello",
        jwt_token="user-jwt",
        owner="user-123",
        owner_name="Test User",
        owner_email="test@example.com",
    )

    # Assert that DocumentIndexContext was created with correct owner details
    assert len(captured_context) == 1
    context = captured_context[0]
    assert isinstance(context, DocumentIndexContext)
    assert context.owner == "user-123"
    assert context.owner_name == "Test User"
    assert context.owner_email == "test@example.com"

    headers = mock_langflow_chat.call_args.kwargs["extra_headers"]
    assert headers["X-LANGFLOW-GLOBAL-VAR-JWT"] == "user-jwt"
    assert headers["X-Langflow-Global-Var-OPENRAG_RETRIEVAL_URL"].endswith("/search")
    assert headers["X-Langflow-Global-Var-OPENRAG_QUERY_FILTER"] == (
        '{"filters": {"data_sources": ["archive.pdf"]}, "limit": 4, "scoreThreshold": 0.25}'
    )


@pytest.mark.asyncio
async def test_langflow_chat_keeps_explicit_exhaustive_wording_on_normal_search_path(monkeypatch):
    fake_langflow_client = MagicMock()
    monkeypatch.setattr(
        "config.settings.clients.ensure_langflow_client",
        AsyncMock(return_value=fake_langflow_client),
    )
    monkeypatch.setattr(
        "utils.langflow_headers.add_provider_credentials_to_headers",
        AsyncMock(),
    )
    mock_langflow_chat = AsyncMock(return_value=("response", "response-id", [], None))
    monkeypatch.setattr("agent.async_langflow_chat", mock_langflow_chat)
    monkeypatch.setattr(
        "services.langflow_ingest_token_service.LangflowIngestTokenService.create_token",
        lambda _self, _context: "fake-ingest-token",
    )
    set_search_filters({})

    await ChatService().langflow_chat(
        prompt="Je veux tous les mails : fais une recherche exhaustive",
        jwt_token="user-jwt",
    )

    headers = mock_langflow_chat.call_args.kwargs["extra_headers"]
    context = json.loads(headers["X-Langflow-Global-Var-OPENRAG_QUERY_FILTER"])
    assert context == {"filters": {}, "limit": 10, "scoreThreshold": 0}


@pytest.mark.asyncio
async def test_streaming_chat_uses_standard_retrieval_progress_without_legacy_audit(monkeypatch):
    fake_langflow_client = MagicMock()
    monkeypatch.setattr(
        "config.settings.clients.ensure_langflow_client",
        AsyncMock(return_value=fake_langflow_client),
    )
    monkeypatch.setattr(
        "utils.langflow_headers.add_provider_credentials_to_headers",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "services.langflow_ingest_token_service.LangflowIngestTokenService.create_token",
        lambda _self, _context: "fake-ingest-token",
    )
    captured: dict = {}

    async def fake_stream(*_args, **kwargs):
        captured["headers"] = kwargs["extra_headers"]
        yield b'{"type":"response.output_text.delta","delta":"answer"}\n'

    monkeypatch.setattr("agent.async_langflow_chat_stream", fake_stream)
    set_search_filters({})

    stream = await ChatService().langflow_chat(
        prompt="Fais une recherche exhaustive sur toute l'archive",
        user_id="test-user",
        jwt_token="user-jwt",
        stream=True,
    )
    chunks = [json.loads(chunk) async for chunk in stream]

    context = json.loads(captured["headers"]["X-Langflow-Global-Var-OPENRAG_QUERY_FILTER"])
    assert context["filters"] == {}
    assert context["limit"] == 10
    assert context["scoreThreshold"] == 0
    assert len(context["progressId"]) == 32
    assert not any(str(item.get("type", "")).startswith("openrag.audit.") for item in chunks)
    assert any(item.get("type") == "openrag.retrieval.progress" for item in chunks)
    assert any(item.get("delta") == "answer" for item in chunks)


@pytest.mark.asyncio
async def test_non_streaming_chat_hydrates_complete_source_from_cited_chunk(monkeypatch):
    fake_langflow_client = MagicMock()
    monkeypatch.setattr(
        "config.settings.clients.ensure_langflow_client",
        AsyncMock(return_value=fake_langflow_client),
    )
    monkeypatch.setattr(
        "utils.langflow_headers.add_provider_credentials_to_headers",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "agent.async_langflow_chat",
        AsyncMock(
            return_value=(
                "Verified answer. (Source: TEST_CHUNK_ID)",
                "response-id",
                [{"chunk_id": "TEST_CHUNK_ID", "filename": ""}],
                None,
            )
        ),
    )
    hydrated_source = {
        "filename": "invoice.pdf",
        "text": "verified evidence",
        "mimetype": "application/pdf",
        "page": 1,
        "source_url": "/api/source-files/TEST_DOCUMENT_ID.token",
        "document_id": "TEST_DOCUMENT_ID",
        "chunk_id": "TEST_CHUNK_ID",
        "chunk_index": 2,
        "chunking_strategy": "hybrid",
    }
    search_service = SimpleNamespace(resolve_cited_chunks=AsyncMock(return_value=[hydrated_source]))
    set_search_filters({})
    chat_svc = ChatService(search_service=search_service)

    response = await chat_svc.langflow_chat(
        prompt="question",
        user_id="user-42",
        jwt_token="jwt-42",
    )

    assert response["sources"] == [
        {
            "filename": "invoice.pdf",
            "text": "verified evidence",
            "score": 0,
            "page": 1,
            "mimetype": "application/pdf",
            "chunk_id": "TEST_CHUNK_ID",
            "id": "TEST_CHUNK_ID",
            "embedding_model": None,
            "parser": None,
            "chunk_size": None,
            "chunk_overlap": None,
            "source_url": "/api/source-files/TEST_DOCUMENT_ID.token",
            "document_id": "TEST_DOCUMENT_ID",
            "chunk_index": 2,
            "chunking_strategy": "hybrid",
        }
    ]
    search_service.resolve_cited_chunks.assert_awaited_once_with(
        ["TEST_CHUNK_ID"],
        user_id="user-42",
        jwt_token="jwt-42",
        filters={},
    )


@pytest.mark.asyncio
async def test_upload_context_chat_passes_owner_metadata(monkeypatch):
    # Mock settings / clients dependencies
    fake_langflow_client = MagicMock()
    monkeypatch.setattr(
        "config.settings.clients.ensure_langflow_client",
        AsyncMock(return_value=fake_langflow_client),
    )
    monkeypatch.setattr(
        "utils.langflow_headers.add_provider_credentials_to_headers",
        AsyncMock(),
    )

    # Mock async_langflow to prevent actual network/langflow calls
    monkeypatch.setattr(
        "services.chat_service.async_langflow",
        AsyncMock(return_value=("some response", "response-id")),
    )

    # Capture the context passed to LangflowIngestTokenService.create_token
    captured_context = []

    def mock_create_token(self, context):
        captured_context.append(context)
        return "fake-ingest-token"

    monkeypatch.setattr(
        "services.langflow_ingest_token_service.LangflowIngestTokenService.create_token",
        mock_create_token,
    )

    # Instantiate ChatService and invoke upload_context_chat with specific owner metadata
    chat_svc = ChatService()
    await chat_svc.upload_context_chat(
        document_content="content",
        filename="doc.txt",
        owner="user-456",
        owner_name="Another User",
        owner_email="another@example.com",
        endpoint="langflow",
    )

    # Assert that DocumentIndexContext was created with correct owner details
    assert len(captured_context) == 1
    context = captured_context[0]
    assert isinstance(context, DocumentIndexContext)
    assert context.owner == "user-456"
    assert context.owner_name == "Another User"
    assert context.owner_email == "another@example.com"
