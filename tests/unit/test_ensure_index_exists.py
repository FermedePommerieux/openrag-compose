from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.documents import _ensure_index_exists


@pytest.mark.asyncio
async def test_ensure_index_exists_does_not_reinitialize_existing_index(monkeypatch):
    """Routine ingestion must not rewrite cluster security configuration."""
    client = SimpleNamespace(indices=SimpleNamespace(exists=AsyncMock(return_value=True)))
    init_index = AsyncMock()

    monkeypatch.setattr("config.settings.get_index_name", lambda: "documents")
    monkeypatch.setattr(
        "config.settings.clients.create_index_admin_opensearch_client",
        lambda _token: client,
    )
    monkeypatch.setattr("main.init_index", init_index)

    await _ensure_index_exists("service-token")

    client.indices.exists.assert_awaited_once_with(index="documents")
    init_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_index_exists_initializes_missing_index(monkeypatch):
    """A reset or fresh cluster still follows the complete initialization path."""
    client = SimpleNamespace(indices=SimpleNamespace(exists=AsyncMock(return_value=False)))
    init_index = AsyncMock()

    monkeypatch.setattr("config.settings.get_index_name", lambda: "documents")
    monkeypatch.setattr(
        "config.settings.clients.create_index_admin_opensearch_client",
        lambda _token: client,
    )
    monkeypatch.setattr("main.init_index", init_index)

    await _ensure_index_exists("service-token")

    client.indices.exists.assert_awaited_once_with(index="documents")
    init_index.assert_awaited_once_with(client)
