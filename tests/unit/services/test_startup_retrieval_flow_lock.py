"""Startup must not continue with an unverified unlocked system flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_startup_fails_closed_when_retrieval_flow_lock_cannot_be_restored(monkeypatch):
    import services.startup_orchestrator as orchestrator

    monkeypatch.setattr("config.settings.IBM_AUTH_ENABLED", True, raising=False)
    monkeypatch.setattr(orchestrator, "FETCH_OPENRAG_DOCS_AT_STARTUP", False)

    flows_service = MagicMock()
    flows_service.ensure_flows_exist = AsyncMock(return_value=set())
    flows_service.migrate_persisted_retrieval_flow = AsyncMock(
        return_value={"status": "lock_restore_failed", "flow_id": "system-flow"}
    )
    services = {
        "workspace_config_service": MagicMock(),
        "models_service": MagicMock(update_model_registry=AsyncMock()),
        "document_service": MagicMock(),
        "task_service": MagicMock(),
        "langflow_file_service": MagicMock(),
        "session_manager": MagicMock(),
        "langflow_mcp_service": MagicMock(),
        "flows_service": flows_service,
    }

    with (
        patch.object(orchestrator.TelemetryClient, "send_event", AsyncMock()),
        patch.object(
            orchestrator,
            "_reingest_default_docs_on_upgrade_if_needed",
            AsyncMock(return_value=False),
        ),
        patch.object(orchestrator, "_update_mcp_server_urls", AsyncMock()),
        patch.object(
            orchestrator,
            "get_openrag_config",
            return_value=SimpleNamespace(agent=SimpleNamespace(system_prompt="custom")),
        ),
    ):
        with pytest.raises(RuntimeError, match="could not be re-locked"):
            await orchestrator.startup_tasks(services)

    flows_service.ensure_flows_exist.assert_awaited_once()
    flows_service.migrate_persisted_retrieval_flow.assert_awaited_once()
