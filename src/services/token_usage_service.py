"""Token and cost accounting for one provider response.

OpenAI returns authoritative usage on each model response. Costs are calculated
per response, which preserves the >272k GPT-5.6 pricing tier instead of applying
a rate to a lossy aggregate afterwards.

Rates are USD per one million tokens, verified against the official OpenAI
model pages on 2026-08-27. Unknown models remain metered but have ``cost_usd``
set to ``None`` rather than inventing a price.
"""

from __future__ import annotations

from typing import Any

_TEXT_RATES: dict[str, tuple[float, float, float]] = {
    # input, cached input, output
    "gpt-5.6-sol": (4.0, 0.4, 20.0),
    "gpt-5.6": (4.0, 0.4, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 1.2),
    "gpt-5.5": (5.0, 0.5, 30.0),
}
_EMBEDDING_RATES: dict[str, float] = {
    "text-embedding-3-large": 0.13,
    "text-embedding-3-small": 0.02,
}
_LONG_CONTEXT_THRESHOLD = 272_000


def _value(source: Any, name: str, default: Any = 0) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class TokenUsageService:
    """Price authoritative usage returned by a provider response."""

    def describe_usage(self, model: str, usage: Any) -> dict[str, Any]:
        """Return one response's usage plus its reproducible public-price estimate."""
        input_tokens = int(_value(usage, "input_tokens", _value(usage, "prompt_tokens", 0)) or 0)
        output_tokens = int(
            _value(usage, "output_tokens", _value(usage, "completion_tokens", 0)) or 0
        )
        total_tokens = int(_value(usage, "total_tokens", input_tokens + output_tokens) or 0)
        input_details = _value(usage, "input_tokens_details", {}) or {}
        output_details = _value(usage, "output_tokens_details", {}) or {}
        cached_tokens = int(_value(input_details, "cached_tokens", 0) or 0)
        reasoning_tokens = int(_value(output_details, "reasoning_tokens", 0) or 0)
        cost = self._calculate_cost(
            model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "cost_usd": cost,
            "cost_complete": cost is not None,
            "pricing_basis": "OpenAI public API rates verified 2026-08-27",
            "calls": 1,
            "models": {
                model: {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "calls": 1,
                }
            },
        }

    @staticmethod
    def _calculate_cost(
        model: str, *, input_tokens: int, cached_tokens: int, output_tokens: int
    ) -> float | None:
        if model in _EMBEDDING_RATES:
            return round(input_tokens * _EMBEDDING_RATES[model] / 1_000_000, 8)
        rates = _TEXT_RATES.get(model)
        if rates is None:
            return None
        input_rate, cached_rate, output_rate = rates
        uncached_tokens = max(0, input_tokens - cached_tokens)
        input_multiplier = 2.0 if input_tokens > _LONG_CONTEXT_THRESHOLD else 1.0
        output_multiplier = 1.5 if input_tokens > _LONG_CONTEXT_THRESHOLD else 1.0
        token_dollars = (
            (uncached_tokens * input_rate * input_multiplier)
            + (cached_tokens * cached_rate * input_multiplier)
            + (output_tokens * output_rate * output_multiplier)
        )
        return round(token_dollars / 1_000_000, 8)


token_usage_service = TokenUsageService()
