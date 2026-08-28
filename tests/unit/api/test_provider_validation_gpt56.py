from unittest.mock import AsyncMock

import pytest

from api import provider_validation


class _HTTPResponse:
    status_code = 200


class _HTTPClient:
    def __init__(self, post: AsyncMock):
        self.post = post

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_gpt_56_tool_validation_uses_responses(monkeypatch):
    post = AsyncMock(return_value=_HTTPResponse())
    monkeypatch.setattr(
        provider_validation.httpx,
        "AsyncClient",
        lambda: _HTTPClient(post),
    )

    await provider_validation._test_openai_completion_with_tools("secret", "gpt-5.6-sol")

    request = post.await_args
    assert request.args[0] == "https://api.openai.com/v1/responses"
    assert request.kwargs["json"]["reasoning"] == {"effort": "low"}
    assert request.kwargs["json"]["tools"][0]["type"] == "function"
    assert "messages" not in request.kwargs["json"]


@pytest.mark.asyncio
async def test_other_openai_models_keep_chat_completions(monkeypatch):
    post = AsyncMock(return_value=_HTTPResponse())
    monkeypatch.setattr(
        provider_validation.httpx,
        "AsyncClient",
        lambda: _HTTPClient(post),
    )

    await provider_validation._test_openai_completion_with_tools("secret", "gpt-5.5")

    request = post.await_args
    assert request.args[0] == "https://api.openai.com/v1/chat/completions"
    assert "messages" in request.kwargs["json"]
    assert "reasoning" not in request.kwargs["json"]
