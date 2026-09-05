"""Planner settings must persist independently and support durable chat fallback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from starlette.requests import Request

import api.settings.endpoints as settings_api
from config.config_manager import ConfigManager, OpenRAGConfig
from services.workspace_config_service import WorkspaceConfigService


@pytest_asyncio.fixture
async def planner_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENRAG_STORAGE_MODE", "db")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    manager = ConfigManager(config_file=str(tmp_path / "config.yaml"))
    workspace = WorkspaceConfigService(manager, async_sessionmaker(engine, expire_on_commit=False))
    config = OpenRAGConfig.from_dict(
        {
            "edited": True,
            "agent": {"llm_provider": "openai", "llm_model": "chat-model"},
            "providers": {"ollama": {"endpoint": "http://localhost:11434", "configured": True}},
        }
    )
    await workspace.save_config(config)
    monkeypatch.setattr(settings_api, "config_manager", manager)
    monkeypatch.setattr(settings_api, "get_openrag_config", manager.get_config)
    monkeypatch.setattr(settings_api, "LANGFLOW_INGEST_FLOW_ID", "")
    monkeypatch.setattr("services.local_source_service.is_local_storage_available", lambda: False)
    monkeypatch.setattr(settings_api, "validate_provider_setup", AsyncMock())
    monkeypatch.setattr(settings_api.clients, "refresh_patched_client", AsyncMock())
    monkeypatch.setattr(settings_api.TelemetryClient, "send_event", AsyncMock())
    monkeypatch.setattr(settings_api, "_run_async_post_save_langflow_updates", AsyncMock())
    rbac = SimpleNamespace(has_permission=AsyncMock(return_value=True), audit_denied=AsyncMock())
    user = SimpleNamespace(db_user_id="planner-admin", user_id="planner-admin")
    yield workspace, rbac, user
    await workspace.await_pending_mirrors()
    if settings_api._background_tasks:
        await asyncio.gather(*settings_api._background_tasks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_planner_selection_reset_and_later_chat_change_survive_database_reload(
    planner_settings,
):
    workspace, rbac, user = planner_settings

    async def update(**fields):
        result = await settings_api.update_settings(
            settings_api.SettingsUpdateBody(**fields), user=user, rbac=rbac
        )
        assert isinstance(result, settings_api.SettingsUpdateResponse)
        await workspace.await_pending_mirrors()
        await workspace.load_config()

    async def read_planner():
        response = await settings_api.get_settings(
            Request({"type": "http", "headers": []}), user=user, rbac=rbac
        )
        return response.planner

    await update(planner_provider="ollama", planner_model="small-planner")
    selected = await read_planner()
    assert (selected.llm_provider, selected.llm_model) == ("ollama", "small-planner")
    assert selected.configured_source == "workspace_config.agent.planner"
    assert settings_api.get_openrag_config().agent.llm_model == "chat-model"
    settings_api.validate_provider_setup.assert_awaited_once()

    # Clearing must work even if the old provider is now unavailable.
    settings_api.validate_provider_setup.side_effect = RuntimeError("provider offline")
    await update(planner_provider="", planner_model="")
    fallback = await read_planner()
    assert (fallback.llm_provider, fallback.llm_model) == ("openai", "chat-model")
    assert fallback.configured_source == "workspace_config.agent.agent_fallback"
    settings_api.validate_provider_setup.assert_awaited_once()

    # A restart and a later chat change must not resurrect the old explicit model.
    config = await workspace.load_config()
    assert config.agent.planner_provider == config.agent.planner_model == ""
    config.agent.llm_model = "later-chat-model"
    await workspace.save_config(config)
    await workspace.load_config()
    assert (await read_planner()).llm_model == "later-chat-model"


@pytest.mark.asyncio
async def test_planner_reset_requires_provider_write_permission(planner_settings):
    _, rbac, user = planner_settings
    rbac.has_permission.return_value = False
    with pytest.raises(HTTPException) as error:
        await settings_api.update_settings(
            settings_api.SettingsUpdateBody(planner_provider="", planner_model=""),
            user=user,
            rbac=rbac,
        )
    assert error.value.status_code == 403
    rbac.audit_denied.assert_awaited_once_with("planner-admin", "providers:write")
    settings_api.validate_provider_setup.assert_not_awaited()


@pytest.mark.parametrize(
    "fields",
    [
        {"planner_model": ""},
        {"planner_provider": ""},
        {"planner_provider": "openai", "planner_model": "  "},
        {"planner_provider": "", "planner_model": "small-planner"},
    ],
)
def test_partial_or_ambiguous_planner_clear_is_rejected(fields):
    with pytest.raises(ValidationError, match="Clear planner_model and planner_provider together"):
        settings_api.SettingsUpdateBody(**fields)
