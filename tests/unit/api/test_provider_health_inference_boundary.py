import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import provider_health, provider_validation
from utils import provider_health_cache


def _config():
    provider = SimpleNamespace(api_key="secret", endpoint=None, project_id=None)
    return SimpleNamespace(
        agent=SimpleNamespace(llm_provider="openai", llm_model="gpt-5.6-sol"),
        knowledge=SimpleNamespace(
            embedding_provider="openai",
            embedding_model="text-embedding-3-large",
        ),
        get_llm_provider_config=lambda: provider,
        get_embedding_provider_config=lambda: provider,
    )


@pytest.mark.asyncio
async def test_automatic_health_poll_never_enables_paid_inference(monkeypatch):
    provider_health_cache.invalidate()
    validate = AsyncMock()
    monkeypatch.setattr(provider_health, "get_openrag_config", _config)
    monkeypatch.setattr(provider_health, "validate_provider_setup", validate)

    response = await provider_health.check_provider_health(
        provider=None,
        test_completion=True,
        user=SimpleNamespace(),
    )

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "healthy"
    assert validate.await_count == 2
    assert all(call.kwargs["test_completion"] is False for call in validate.await_args_list)


@pytest.mark.asyncio
async def test_explicit_provider_health_may_request_one_full_validation(monkeypatch):
    provider = SimpleNamespace(api_key="secret", endpoint=None, project_id=None)
    config = _config()
    config.providers = SimpleNamespace(get_provider_config=lambda _name: provider)
    validate = AsyncMock()
    monkeypatch.setattr(provider_health, "get_openrag_config", lambda: config)
    monkeypatch.setattr(provider_health, "validate_provider_setup", validate)

    response = await provider_health.check_provider_health(
        provider="openai",
        test_completion=True,
        user=SimpleNamespace(),
    )

    assert response.status_code == 200
    validate.assert_awaited_once()
    assert validate.await_args.kwargs["test_completion"] is True


class _HTTPResponse:
    status_code = 200


class _HTTPClient:
    def __init__(self, captured):
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.captured.append((url, kwargs))
        return _HTTPResponse()


@pytest.mark.asyncio
async def test_gpt_56_explicit_tool_validation_uses_responses(monkeypatch):
    captured = []
    monkeypatch.setattr(
        provider_validation.httpx,
        "AsyncClient",
        lambda: _HTTPClient(captured),
    )

    await provider_validation._test_openai_completion_with_tools("secret", "gpt-5.6-sol")

    assert len(captured) == 1
    url, request = captured[0]
    assert url == "https://api.openai.com/v1/responses"
    assert request["json"]["reasoning"] == {"effort": "low"}
    assert request["json"]["tools"][0]["type"] == "function"
    assert "messages" not in request["json"]
