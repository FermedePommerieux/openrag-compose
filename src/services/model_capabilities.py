"""Provider/model capabilities used to build inference requests.

Keep request construction here so product paths do not learn capabilities by
retrying failed calls.  Rules describe model families, not benchmark-specific
exceptions, and can be extended as providers add or remove request parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelRequestCapabilities:
    """Known request-parameter restrictions for one provider/model family."""

    unsupported_responses_parameters: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _CapabilityRule:
    provider: str
    model_pattern: re.Pattern[str]
    capabilities: ModelRequestCapabilities


_RESPONSES_CAPABILITY_RULES = (
    _CapabilityRule(
        provider="openai",
        model_pattern=re.compile(r"^gpt-5\.6(?:-|$)", re.IGNORECASE),
        capabilities=ModelRequestCapabilities(
            unsupported_responses_parameters=frozenset({"temperature"})
        ),
    ),
)


def _normalized_provider_and_model(provider: str | None, model: str) -> tuple[str, str]:
    normalized_provider = str(provider or "").strip().casefold()
    normalized_model = str(model or "").strip()
    if "/" in normalized_model:
        prefix, unqualified_model = normalized_model.split("/", 1)
        if not normalized_provider:
            normalized_provider = prefix.casefold()
        if prefix.casefold() == normalized_provider:
            normalized_model = unqualified_model
    return normalized_provider, normalized_model


def model_request_capabilities(*, provider: str | None, model: str) -> ModelRequestCapabilities:
    """Resolve declared capabilities for a provider-qualified model."""

    normalized_provider, normalized_model = _normalized_provider_and_model(provider, model)
    for rule in _RESPONSES_CAPABILITY_RULES:
        if rule.provider == normalized_provider and rule.model_pattern.match(normalized_model):
            return rule.capabilities
    return ModelRequestCapabilities()


def model_capability_profile(*, provider: str | None, model: str) -> dict[str, Any]:
    """Return a stable, non-secret identity for the resolved request contract."""

    capabilities = model_request_capabilities(provider=provider, model=model)
    unsupported = sorted(capabilities.unsupported_responses_parameters)
    return {
        "registry": "responses-model-capabilities-v1",
        "unsupported_responses_parameters": unsupported,
    }


def resolve_planner_selection(config: Any) -> tuple[str, str, str]:
    """Resolve the runtime planner selection and describe its persisted source."""

    agent = getattr(config, "agent", None)
    planner_provider = str(getattr(agent, "planner_provider", "") or "").strip()
    planner_model = str(getattr(agent, "planner_model", "") or "").strip()
    if planner_provider and planner_model:
        return planner_provider, planner_model, "workspace_config.agent.planner"
    return (
        planner_provider or str(getattr(agent, "llm_provider", "") or "").strip(),
        planner_model or str(getattr(agent, "llm_model", "") or "").strip(),
        "workspace_config.agent.agent_fallback",
    )


def build_responses_request(
    *,
    provider: str | None,
    model: str,
    input: str,
    stream: bool,
    **optional_parameters: Any,
) -> dict[str, Any]:
    """Build a Responses API request containing only supported parameters."""

    capabilities = model_request_capabilities(provider=provider, model=model)
    request: dict[str, Any] = {"model": model, "input": input, "stream": stream}
    request.update(
        {
            name: value
            for name, value in optional_parameters.items()
            if value is not None and name not in capabilities.unsupported_responses_parameters
        }
    )
    return request
