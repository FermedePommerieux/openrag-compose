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
async def test_product_planner_uses_capability_aware_request_for_runtime_model(monkeypatch):
    from services import search_service

    create = AsyncMock(
        return_value=SimpleNamespace(
            output_text='{"queries":[{"text":"Alpha contract correspondence",'
            '"kind":"documentary_subject"}]}'
        )
    )
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
            agent=SimpleNamespace(llm_model="gpt-5.6-sol", llm_provider="openai")
        ),
    )
    models_service = SimpleNamespace(
        get_litellm_model_name=AsyncMock(return_value="openai/gpt-5.6-sol")
    )
    service = SearchService.__new__(SearchService)
    service.models_service = models_service

    plan, error, _elapsed, audit = await service._generate_discovery_plan(
        "all records about contract Alpha",
        max_queries=4,
    )

    assert error is None
    assert len(plan) == 2
    request = create.await_args.kwargs
    assert request["model"] == "openai/gpt-5.6-sol"
    assert request["max_output_tokens"] == 800
    assert "temperature" not in request
    assert audit["planner_invoked"] is True
    assert audit["request_parameters"]["model"] == "openai/gpt-5.6-sol"
    assert audit["request_parameters"]["max_output_tokens"] == 800
    assert audit["request_fingerprint"]
    assert audit["plan_fingerprint"]
