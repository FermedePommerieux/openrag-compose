from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from config.config_manager import ConfigManager
from services.model_capabilities import resolve_planner_selection
from services.runtime_behavior import (
    assert_runtime_behavior_match,
    build_runtime_behavior_profile,
)


class _ObservedFlow:
    def __init__(
        self,
        *,
        prompt: str = "configured prompt",
        provider: str = "openai",
        model: str = "gpt-5.4-mini",
    ) -> None:
        self.behavior = {
            "flow_id": "managed-flow",
            "flow_version": 13,
            "locked": True,
            "prompt": prompt,
            "agent_provider": provider,
            "agent_model": model,
            "agent_guard_version": 1,
            "agent_code_sha256": "agent-code-sha",
            "langgraph_max_iterations": 15,
        }

    async def get_chat_flow_behavior(self):
        return self.behavior


def _config():
    return SimpleNamespace(
        agent=SimpleNamespace(
            llm_provider="openai",
            llm_model="gpt-5.4-mini",
            planner_provider="openai",
            planner_model="gpt-5.6-sol",
            system_prompt="configured prompt",
        ),
        knowledge=SimpleNamespace(
            embedding_provider="openai",
            embedding_model="text-embedding-3-large",
            retrieval_strategy="rrf",
            retrieval_mode="hybrid",
            retrieval_lexical_candidates=50,
            retrieval_vector_candidates=50,
            retrieval_rrf_k=60,
            retrieval_max_chunks_per_document=3,
            retrieval_adaptive_max_chunks_per_document=20,
            retrieval_reranker_url="",
            retrieval_reranker_timeout=5,
            retrieval_debug=False,
            retrieval_scope_seed_count=100,
            retrieval_scope_max_depth=8,
            retrieval_scope_max_entities=500,
            retrieval_scope_max_documents=250,
            retrieval_scope_batch_size=50,
        ),
    )


@pytest.mark.asyncio
async def test_runtime_profile_is_deterministic_complete_and_secret_free():
    config = _config()
    config.providers = SimpleNamespace(openai=SimpleNamespace(api_key="must-not-leak"))
    first = await build_runtime_behavior_profile(config, _ObservedFlow())
    second = await build_runtime_behavior_profile(config, _ObservedFlow())

    assert first["status"] == "MATCH"
    assert first["runtime_behavior_fingerprint"] == second["runtime_behavior_fingerprint"]
    assert first["agent"]["configured_model"] == "gpt-5.4-mini"
    assert first["planner"]["configured_model"] == "gpt-5.6-sol"
    assert first["retrieval"]["seed_budget"] == 100
    assert first["scope"]["policy_id"] == "documentary-prov-o"
    assert first["multi_query"]["enabled_default"] is False
    assert first["agent_guard"]["retrieval_guard_version"] == 1
    assert "must-not-leak" not in json.dumps(first)
    assert all(
        item["status"] == "PASS" and item["evidence_sha256"]
        for item in first["validation_evidence"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "effective_planner", "failed_check"),
    [
        (_ObservedFlow(prompt="sabotaged"), None, "runtime_prompt_match"),
        (
            _ObservedFlow(provider="anthropic", model="claude-opus-5"),
            None,
            "runtime_agent_model_match",
        ),
        (
            _ObservedFlow(),
            ("openai", "gpt-5.4-mini"),
            "runtime_planner_model_match",
        ),
    ],
)
async def test_sabotaged_effective_behavior_is_detected(flow, effective_planner, failed_check):
    profile = await build_runtime_behavior_profile(
        _config(), flow, effective_planner=effective_planner
    )

    assert profile["status"] == "MISMATCH"
    evidence = {item["check_id"]: item for item in profile["validation_evidence"]}
    assert evidence[failed_check]["status"] == "FAIL"
    with pytest.raises(RuntimeError, match="managed runtime mismatch detected"):
        assert_runtime_behavior_match(profile)


@pytest.mark.asyncio
async def test_agent_and_planner_runtime_selections_are_independent():
    profile = await build_runtime_behavior_profile(_config(), _ObservedFlow())

    assert profile["agent"]["effective_model"] == "gpt-5.4-mini"
    assert profile["planner"]["effective_model"] == "gpt-5.6-sol"
    assert profile["agent"]["match"] is True
    assert profile["planner"]["match"] is True


def test_explicit_planner_selection_does_not_follow_later_agent_changes():
    config = _config()
    before = resolve_planner_selection(config)
    config.agent.llm_model = "gpt-5.6-terra"
    after = resolve_planner_selection(config)

    assert (
        before
        == after
        == (
            "openai",
            "gpt-5.6-sol",
            "workspace_config.agent.planner",
        )
    )


def test_deployment_environment_cannot_override_functional_llm_settings(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime-config.yaml"
    config_path.write_text(
        """edited: false
agent:
  llm_provider: openai
  llm_model: gpt-runtime
  planner_provider: openai
  planner_model: gpt-planner-runtime
  system_prompt: runtime prompt
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "deployment-model")
    monkeypatch.setenv("SYSTEM_PROMPT", "deployment prompt")

    config = ConfigManager(str(config_path)).load_config()

    assert config.agent.llm_provider == "openai"
    assert config.agent.llm_model == "gpt-runtime"
    assert config.agent.planner_model == "gpt-planner-runtime"
    assert config.agent.system_prompt == "runtime prompt"
