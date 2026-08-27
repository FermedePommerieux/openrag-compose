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


def test_accumulates_reasoning_embedding_and_cached_cost_per_audit() -> None:
    service = TokenUsageService()
    service.reset("audit-1")

    with service.scope("audit-1"):
        service.record_usage("gpt-5.6-sol", _usage(1_000, 100, cached=400, reasoning=50))
        service.record_usage("text-embedding-3-large", _usage(2_000, 0))

    result = service.snapshot("audit-1")
    assert result["input_tokens"] == 3_000
    assert result["output_tokens"] == 100
    assert result["input_tokens_details"]["cached_tokens"] == 400
    assert result["output_tokens_details"]["reasoning_tokens"] == 50
    assert result["calls"] == 2
    # Sol: 600*4 + 400*.4 + 100*20 = $0.00456; embedding: $0.00026.
    assert result["cost_usd"] == pytest.approx(0.00482)
    assert result["cost_complete"] is True


def test_applies_long_context_multiplier_to_each_call_not_aggregate() -> None:
    service = TokenUsageService()
    described = service.describe_usage("gpt-5.6-terra", _usage(300_000, 1_000))
    assert described["cost_usd"] == pytest.approx(1.218)


def test_unknown_model_keeps_tokens_but_refuses_to_invent_cost() -> None:
    service = TokenUsageService()
    service.reset("audit-unknown")
    service.record_usage("private-model", _usage(10, 5), audit_id="audit-unknown")
    result = service.snapshot("audit-unknown")
    assert result["total_tokens"] == 15
    assert result["cost_usd"] is None
    assert result["cost_complete"] is False


def test_application_cache_reports_avoided_usage_without_billing_it() -> None:
    service = TokenUsageService()
    service.reset("audit-cache")

    service.record_application_cache_hit(
        "gpt-5.6-luna",
        {
            "input_tokens": 10_000,
            "output_tokens": 500,
            "total_tokens": 10_500,
            "cost_usd": 0.0008,
        },
        audit_id="audit-cache",
    )

    result = service.snapshot("audit-cache")
    assert result["calls"] == 0
    assert result["total_tokens"] == 0
    assert result["cost_usd"] == 0.0
    assert result["application_cache"] == {
        "hits": 1,
        "avoided_provider_calls": 1,
        "avoided_input_tokens": 10_000,
        "avoided_output_tokens": 500,
        "avoided_total_tokens": 10_500,
        "avoided_cost_usd": 0.0008,
    }
