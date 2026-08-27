from unittest.mock import AsyncMock

import httpx
import pytest

from config import settings


@pytest.mark.asyncio
async def test_langflow_openai_client_uses_configured_timeout(monkeypatch):
    """The OpenAI SDK's 600s default must not cancel long audit tools."""

    monkeypatch.setattr(settings, "LANGFLOW_KEY", "test-key")
    monkeypatch.setattr(settings, "LANGFLOW_URL", "http://langflow.test")
    monkeypatch.setattr(settings, "LANGFLOW_TIMEOUT", 2_400.0)
    monkeypatch.setattr(settings, "LANGFLOW_STREAM_TIMEOUT", 21_600.0)
    monkeypatch.setattr(settings, "LANGFLOW_CONNECT_TIMEOUT", 30.0)
    monkeypatch.setattr(settings, "get_langflow_api_key", AsyncMock(return_value="test-key"))
    clients = settings.AppClients()

    client = await clients.ensure_langflow_client()
    try:
        assert client is not None
        assert isinstance(client.timeout, httpx.Timeout)
        assert client.timeout.read == 21_600.0
        assert client.timeout.connect == 30.0
    finally:
        if client is not None:
            await client.close()
