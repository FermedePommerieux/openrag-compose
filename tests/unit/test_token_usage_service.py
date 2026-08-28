from types import SimpleNamespace

import pytest

from services.token_usage_service import TokenUsageService


def _usage(input_tokens: int, output_tokens: int, *, cached: int = 0, reasoning: int = 0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def test_prices_reasoning_cached_input_and_embeddings_per_response() -> None:
    service = TokenUsageService()
    sol = service.describe_usage(
        "gpt-5.6-sol",
        _usage(1_000, 100, cached=400, reasoning=50),
    )
    embedding = service.describe_usage("text-embedding-3-large", _usage(2_000, 0))

    assert sol["input_tokens_details"]["cached_tokens"] == 400
    assert sol["output_tokens_details"]["reasoning_tokens"] == 50
    assert sol["cost_usd"] == pytest.approx(0.00456)
    assert embedding["cost_usd"] == pytest.approx(0.00026)


def test_applies_long_context_multiplier_to_each_call_not_aggregate() -> None:
    service = TokenUsageService()
    described = service.describe_usage("gpt-5.6-terra", _usage(300_000, 1_000))
    assert described["cost_usd"] == pytest.approx(1.218)


def test_unknown_model_keeps_tokens_but_refuses_to_invent_cost() -> None:
    service = TokenUsageService()
    result = service.describe_usage("private-model", _usage(10, 5))
    assert result["total_tokens"] == 15
    assert result["cost_usd"] is None
    assert result["cost_complete"] is False
