from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import api.settings as settings_api
import api.settings.endpoints as settings_endpoints


def _make_config(index_name="documents"):
    return SimpleNamespace(
        edited=True,
        agent=SimpleNamespace(
            llm_provider="openai", llm_model="gpt-4o", system_prompt="original prompt"
        ),
        knowledge=SimpleNamespace(
            embedding_provider="openai",
            embedding_model="text-embedding-3-small",
            index_name=index_name,
        ),
        providers=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_update_settings_rejects_index_name_outside_security_role_patterns(monkeypatch):
    config = _make_config()
    monkeypatch.setattr(settings_endpoints, "get_openrag_config", lambda: config, raising=True)

    with pytest.raises(HTTPException) as exc_info:
        await settings_api.update_settings(
            settings_api.SettingsUpdateBody(index_name="test"),
            session_manager=object(),
            user=None,
        )

    assert exc_info.value.status_code == 422
    # The rejected index name is not permitted, so it must never be written to config.
    assert config.knowledge.index_name == "documents"


@pytest.mark.asyncio
async def test_update_settings_rejects_index_name_without_partially_applying_other_fields(
    monkeypatch,
):
    """A validation failure partway through must not leave earlier fields in
    the same request mutated on the live config object (atomicity)."""
    config = _make_config()
    monkeypatch.setattr(settings_endpoints, "get_openrag_config", lambda: config, raising=True)

    with pytest.raises(HTTPException):
        await settings_api.update_settings(
            settings_api.SettingsUpdateBody(system_prompt="new prompt", index_name="test"),
            session_manager=object(),
            user=None,
        )

    # system_prompt is applied before index_name in the handler, so a pre-fix
    # implementation would have already mutated this field in place.
    assert config.agent.system_prompt == "original prompt"
    assert config.knowledge.index_name == "documents"


@pytest.mark.asyncio
async def test_update_settings_accepts_index_name_matching_security_role_patterns(monkeypatch):
    config = _make_config()
    saved_configs = []
    monkeypatch.setattr(settings_endpoints, "get_openrag_config", lambda: config, raising=True)
    monkeypatch.setattr(
        settings_endpoints.config_manager,
        "save_config_file",
        lambda updated_config: saved_configs.append(updated_config) or True,
        raising=True,
    )

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        settings_endpoints.clients, "_create_langflow_global_variable", _noop, raising=True
    )
    monkeypatch.setattr(settings_endpoints.TelemetryClient, "send_event", _noop, raising=True)
    monkeypatch.setattr(
        settings_endpoints, "_run_async_post_save_langflow_updates", _noop, raising=True
    )
    monkeypatch.setattr(
        settings_endpoints.asyncio, "create_task", lambda coro: coro.close(), raising=True
    )

    await settings_api.update_settings(
        settings_api.SettingsUpdateBody(index_name="documents-v2"),
        session_manager=object(),
        user=None,
    )

    # The staged copy passed to save_config_file carries the new value...
    assert saved_configs[0].knowledge.index_name == "documents-v2"
    # ...but the original config object (the live cache before this call
    # completes) must never be mutated in place.
    assert config.knowledge.index_name == "documents"


@pytest.mark.asyncio
async def test_update_settings_persists_hybrid_and_rrf_configuration(monkeypatch):
    """The Settings UI payload maps directly to the backend-owned config."""
    config = _make_config()
    saved_configs = []
    monkeypatch.setattr(settings_endpoints, "get_openrag_config", lambda: config, raising=True)
    monkeypatch.setattr(
        settings_endpoints.config_manager,
        "save_config_file",
        lambda updated_config: saved_configs.append(updated_config) or True,
        raising=True,
    )

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(settings_endpoints.clients, "refresh_patched_client", _noop, raising=True)
    monkeypatch.setattr(settings_endpoints.TelemetryClient, "send_event", _noop, raising=True)

    await settings_api.update_settings(
        settings_api.SettingsUpdateBody(
            chunking_strategy="hybrid",
            hybrid_max_tokens=768,
            hybrid_merge_peers=False,
            retrieval_strategy="rrf",
            retrieval_mode="hybrid",
            retrieval_lexical_candidates=75,
            retrieval_vector_candidates=80,
            retrieval_rrf_k=42,
            retrieval_max_chunks_per_document=4,
            retrieval_adaptive_max_chunks_per_document=25,
        ),
        session_manager=object(),
        user=None,
    )

    saved = saved_configs[0].knowledge
    assert saved.chunking_strategy == "hybrid"
    assert saved.hybrid_max_tokens == 768
    assert saved.hybrid_merge_peers is False
    assert saved.retrieval_strategy == "rrf"
    assert saved.retrieval_mode == "hybrid"
    assert saved.retrieval_lexical_candidates == 75
    assert saved.retrieval_vector_candidates == 80
    assert saved.retrieval_rrf_k == 42
    assert saved.retrieval_max_chunks_per_document == 4
    assert saved.retrieval_adaptive_max_chunks_per_document == 25


@pytest.mark.parametrize(
    "field,value",
    [
        ("retrieval_lexical_candidates", 0),
        ("retrieval_lexical_candidates", 501),
        ("retrieval_vector_candidates", 0),
        ("retrieval_vector_candidates", 501),
        ("retrieval_rrf_k", 0),
        ("retrieval_rrf_k", 1001),
        ("retrieval_max_chunks_per_document", 0),
        ("retrieval_max_chunks_per_document", 101),
        ("retrieval_adaptive_max_chunks_per_document", 0),
        ("retrieval_adaptive_max_chunks_per_document", 101),
    ],
)
def test_retrieval_settings_reject_backend_out_of_range_values(field, value):
    with pytest.raises(ValidationError):
        settings_api.SettingsUpdateBody(**{field: value})


@pytest.mark.asyncio
async def test_ingestion_capacity_settings_are_persisted_and_applied_live(monkeypatch):
    """The Knowledge Ingest control is backend-owned and hot-reloadable."""
    config = _make_config()
    saved_configs = []
    task_service = SimpleNamespace(reconfigure_ingestion_capacity=AsyncMock())
    monkeypatch.setattr(settings_endpoints, "get_openrag_config", lambda: config, raising=True)
    monkeypatch.setattr(
        settings_endpoints.config_manager,
        "save_config_file",
        lambda updated_config: saved_configs.append(updated_config) or True,
        raising=True,
    )
    monkeypatch.setattr(
        settings_endpoints.clients,
        "refresh_patched_client",
        AsyncMock(),
        raising=True,
    )

    await settings_api.update_settings(
        settings_api.SettingsUpdateBody(
            ingestion_concurrency_mode="auto",
            ingestion_worker_fallback=2,
            ingestion_worker_max=6,
        ),
        session_manager=object(),
        task_service=task_service,
        user=None,
    )

    saved = saved_configs[0].knowledge
    assert saved.ingestion_concurrency_mode == "auto"
    assert saved.ingestion_worker_fallback == 2
    assert saved.ingestion_worker_max == 6
    task_service.reconfigure_ingestion_capacity.assert_awaited_once_with(saved)


@pytest.mark.asyncio
async def test_ingestion_capacity_rejects_fallback_above_maximum(monkeypatch):
    config = _make_config()
    monkeypatch.setattr(settings_endpoints, "get_openrag_config", lambda: config, raising=True)

    with pytest.raises(HTTPException) as exc_info:
        await settings_api.update_settings(
            settings_api.SettingsUpdateBody(
                ingestion_concurrency_mode="auto",
                ingestion_worker_fallback=5,
                ingestion_worker_max=4,
            ),
            session_manager=object(),
            user=None,
        )

    assert exc_info.value.status_code == 422
    assert not hasattr(config.knowledge, "ingestion_concurrency_mode")
