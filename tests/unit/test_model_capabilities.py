from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.model_capabilities import build_responses_request
from services.search_service import SearchService


def test_gpt_56_responses_request_omits_unsupported_temperature():
    request = build_responses_request(
        provider="openai",
        model="openai/gpt-5.6-sol",
        input="plan this query",
        stream=False,
        temperature=0,
        max_output_tokens=800,
    )

    assert request["model"] == "openai/gpt-5.6-sol"
    assert request["max_output_tokens"] == 800
    assert "temperature" not in request


def test_unrestricted_model_keeps_declared_temperature():
    request = build_responses_request(
        provider="openai",
        model="openai/gpt-4o-mini",
        input="plan this query",
        stream=False,
        temperature=0,
    )

    assert request["temperature"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-5.6-sol", "openrag-future-model-canary"])
async def test_openai_planner_uses_native_wire_model_without_litellm_registry(monkeypatch, model):
    import json

    import httpx
    import litellm.utils
    from agentd.patch import patch_openai_with_mcp
    from openai import AsyncOpenAI

    from services import search_service
    from services.models_service import ModelsService

    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == model
        assert "tools" not in body
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1,
                "model": model,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_fixture",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"queries":[{"text":"Alpha contract correspondence","kind":"documentary_subject"}]}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    def reject_registry(*args, **kwargs):
        raise AssertionError("OpenAI query planner must not guess provider from model registry")

    monkeypatch.setattr(litellm.utils, "get_llm_provider", reject_registry)
    async with AsyncOpenAI(
        api_key="fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply))
    ) as client:
        patch_openai_with_mcp(client)
        monkeypatch.setattr(search_service, "clients", SimpleNamespace(patched_llm_client=client))
        monkeypatch.setattr(
            search_service,
            "get_openrag_config",
            lambda: SimpleNamespace(agent=SimpleNamespace(llm_model=model, llm_provider="openai")),
        )
        service = SearchService.__new__(SearchService)
        service.models_service = ModelsService()
        plan, error, _, audit = await service._generate_discovery_plan(
            "all records about contract Alpha", max_queries=4
        )
    assert error is None
    assert len(plan) == 2 and len(requests) == 1
    request = requests[0]
    assert request["max_output_tokens"] == 800
    if model == "gpt-5.6-sol":
        assert "temperature" not in request
    assert audit["planner_invoked"] is True
    assert audit["request_parameters"]["model"] == model
    assert audit["request_fingerprint"] and audit["plan_fingerprint"]


@pytest.mark.asyncio
async def test_other_planner_providers_preserve_their_existing_adapter(monkeypatch):
    from services import search_service

    create = AsyncMock(return_value=SimpleNamespace(output_text='{"queries":[]}'))
    monkeypatch.setattr(
        search_service,
        "clients",
        SimpleNamespace(
            patched_llm_client=SimpleNamespace(responses=SimpleNamespace(create=create))
        ),
    )
    monkeypatch.setattr(
        search_service,
        "get_openrag_config",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(llm_model="local-model", llm_provider="ollama")
        ),
    )
    service = SearchService.__new__(SearchService)
    service.models_service = SimpleNamespace(
        get_litellm_model_name=AsyncMock(return_value="ollama/local-model")
    )
    _, error, _, _ = await service._generate_discovery_plan("fixture", max_queries=4)
    assert error is None
    assert create.await_args.kwargs["model"] == "ollama/local-model"
