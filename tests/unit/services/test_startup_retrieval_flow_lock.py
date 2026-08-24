"""Critical system-flow preparation must reject an unverified unlock."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_critical_flow_preparation_fails_closed_before_prompt_sync(monkeypatch):
    import services.startup_orchestrator as orchestrator

    monkeypatch.setattr("config.settings.IBM_AUTH_ENABLED", True, raising=False)
    monkeypatch.setattr(orchestrator, "FETCH_OPENRAG_DOCS_AT_STARTUP", False)

    flows_service = MagicMock()
    flows_service.ensure_flows_exist = AsyncMock(return_value=set())
    flows_service.migrate_persisted_retrieval_flow = AsyncMock(
        return_value={"status": "lock_restore_failed", "flow_id": "system-flow"}
    )
    flows_service.get_chat_flow_system_prompt = AsyncMock()
    flows_service.update_chat_flow_system_prompt = AsyncMock()
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
        with pytest.raises(RuntimeError, match="lock_restore_failed"):
            await orchestrator.ensure_system_retrieval_flow_ready(services)

    flows_service.ensure_flows_exist.assert_awaited_once()
    flows_service.migrate_persisted_retrieval_flow.assert_awaited_once()
    flows_service.get_chat_flow_system_prompt.assert_not_awaited()
    flows_service.update_chat_flow_system_prompt.assert_not_awaited()
