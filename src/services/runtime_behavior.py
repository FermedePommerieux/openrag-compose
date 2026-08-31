"""Observable RuntimeBehaviorProfile v1 for OpenRAG.

The profile is a snapshot of behavior already owned by application runtime
configuration and the managed Langflow flow. It is not a new configuration
source and never contains provider credentials or other secret values.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from services.model_capabilities import (
    model_capability_profile,
    resolve_planner_selection,
)
from services.retrieval_service import (
    DEFAULT_MULTI_QUERY_CONCURRENCY,
    DEFAULT_MULTI_QUERY_ENABLED,
    MAX_DISCOVERY_QUERIES,
    RetrievalSettings,
    ScopeExhaustiveSettings,
)
from services.scope_traversal_policy import DEFAULT_SCOPE_TRAVERSAL_POLICY

RUNTIME_BEHAVIOR_PROFILE_VERSION = 1


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _validation(
    check_id: str, *, observed: Any, expected: Any, evidence: Any | None = None
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "PASS" if observed == expected else "FAIL",
        "observed": observed,
        "expected": expected,
        "evidence_sha256": _canonical_sha256(
            evidence if evidence is not None else {"observed": observed, "expected": expected}
        ),
    }


def _planner_runtime(config: Any) -> tuple[str, str, str]:
    """Resolve the same planner identity read for each product search request."""

    return resolve_planner_selection(config)


async def build_runtime_behavior_profile(
    config: Any,
    flows_service: Any,
    *,
    effective_planner: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Observe configured and effective runtime behavior and fingerprint it."""

    flow = await flows_service.get_chat_flow_behavior()
    configured_prompt = str(config.agent.system_prompt or "")
    configured_agent_provider = str(config.agent.llm_provider or "").strip().casefold()
    configured_agent_model = str(config.agent.llm_model or "").strip()
    planner_provider, planner_model, planner_source = _planner_runtime(config)
    effective_planner_provider, effective_planner_model = effective_planner or (
        planner_provider,
        planner_model,
    )
    effective_planner_provider = str(effective_planner_provider or "").strip().casefold()
    effective_planner_model = str(effective_planner_model or "").strip()

    configured_prompt_sha = _sha256_text(configured_prompt)
    effective_prompt_sha = _sha256_text(str(flow.get("prompt") or ""))
    effective_agent_provider = str(flow.get("agent_provider") or "").strip().casefold()
    effective_agent_model = str(flow.get("agent_model") or "").strip()

    retrieval = RetrievalSettings.from_knowledge(config.knowledge)
    scope = ScopeExhaustiveSettings.from_knowledge(config.knowledge)
    system_prompt_match = configured_prompt_sha == effective_prompt_sha
    agent_match = (
        configured_agent_provider == effective_agent_provider
        and configured_agent_model == effective_agent_model
    )
    planner_match = (
        planner_provider.casefold() == effective_planner_provider
        and planner_model == effective_planner_model
    )

    profile: dict[str, Any] = {
        "profile_version": RUNTIME_BEHAVIOR_PROFILE_VERSION,
        "status": "MATCH" if system_prompt_match and agent_match and planner_match else "MISMATCH",
        "system_prompt": {
            "configured_source": "workspace_config.agent.system_prompt",
            "configured_content_sha256": configured_prompt_sha,
            "effective_source": f"langflow.managed_flow:{flow.get('flow_id')}:Agent.system_prompt",
            "effective_content_sha256": effective_prompt_sha,
            "match": system_prompt_match,
        },
        "agent": {
            "configured_source": "workspace_config.agent",
            "configured_provider": configured_agent_provider,
            "configured_model": configured_agent_model,
            "effective_source": f"langflow.managed_flow:{flow.get('flow_id')}:Agent.model",
            "effective_provider": effective_agent_provider,
            "effective_model": effective_agent_model,
            "capability_profile": model_capability_profile(
                provider=effective_agent_provider, model=effective_agent_model
            ),
            "match": agent_match,
        },
        "planner": {
            "configured_source": planner_source,
            "configured_provider": planner_provider.casefold(),
            "configured_model": planner_model,
            "effective_source": "backend.search_service.runtime_resolution",
            "effective_provider": effective_planner_provider,
            "effective_model": effective_planner_model,
            "capability_profile": model_capability_profile(
                provider=effective_planner_provider, model=effective_planner_model
            ),
            "match": planner_match,
        },
        "embedding": {
            "source": "workspace_config.knowledge",
            "provider": str(config.knowledge.embedding_provider or "").strip().casefold(),
            "model": str(config.knowledge.embedding_model or "").strip(),
        },
        "retrieval": {
            "source": "workspace_config.knowledge",
            "mode": retrieval.mode,
            "strategy": retrieval.strategy,
            "lexical_candidates": retrieval.lexical_candidates,
            "dense_candidates": retrieval.vector_candidates,
            "rrf_k": retrieval.rrf_k,
            "seed_budget": scope.seed_count,
        },
        "multi_query": {
            "source": "product_search_request_contract",
            "enabled_default": DEFAULT_MULTI_QUERY_ENABLED,
            "max_queries": MAX_DISCOVERY_QUERIES,
            "concurrency": DEFAULT_MULTI_QUERY_CONCURRENCY,
        },
        "scope": {
            "source": "workspace_config.knowledge+scope_traversal_policy",
            "policy_id": DEFAULT_SCOPE_TRAVERSAL_POLICY.policy_id,
            "policy_version": DEFAULT_SCOPE_TRAVERSAL_POLICY.version,
            "seed_count": scope.seed_count,
            "max_depth": scope.max_depth,
            "max_entities": scope.max_entities,
            "max_documents": scope.max_documents,
            "batch_size": scope.batch_size,
        },
        "agent_guard": {
            "source": f"langflow.managed_flow:{flow.get('flow_id')}:Agent.code",
            "retrieval_guard_version": flow.get("agent_guard_version"),
            "langgraph_max_iterations": flow.get("langgraph_max_iterations"),
            "component_code_sha256": flow.get("agent_code_sha256"),
            "flow_version": flow.get("flow_version"),
            "flow_locked": flow.get("locked"),
        },
    }
    effective_behavior = {
        "profile_version": profile["profile_version"],
        "system_prompt_sha256": effective_prompt_sha,
        "agent": {
            "provider": effective_agent_provider,
            "model": effective_agent_model,
            "capability_profile": profile["agent"]["capability_profile"],
        },
        "planner": {
            "provider": effective_planner_provider,
            "model": effective_planner_model,
            "capability_profile": profile["planner"]["capability_profile"],
        },
        "embedding": profile["embedding"],
        "retrieval": profile["retrieval"],
        "multi_query": profile["multi_query"],
        "scope": profile["scope"],
        "agent_guard": profile["agent_guard"],
    }
    profile["runtime_behavior_fingerprint"] = _canonical_sha256(effective_behavior)
    profile["validation_evidence"] = [
        _validation(
            "runtime_prompt_match",
            observed=effective_prompt_sha,
            expected=configured_prompt_sha,
        ),
        _validation(
            "runtime_agent_model_match",
            observed={"provider": effective_agent_provider, "model": effective_agent_model},
            expected={
                "provider": configured_agent_provider,
                "model": configured_agent_model,
            },
        ),
        _validation(
            "runtime_planner_model_match",
            observed={
                "provider": effective_planner_provider,
                "model": effective_planner_model,
            },
            expected={"provider": planner_provider.casefold(), "model": planner_model},
        ),
    ]
    return profile


def assert_runtime_behavior_match(profile: dict[str, Any]) -> None:
    """Fail closed when a managed flow does not execute configured behavior."""

    if profile.get("status") != "MATCH":
        failures = [
            item.get("check_id")
            for item in profile.get("validation_evidence", [])
            if item.get("status") != "PASS"
        ]
        raise RuntimeError(f"managed runtime mismatch detected: {failures}")
