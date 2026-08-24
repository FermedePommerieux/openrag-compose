"""Exercise the real FastAPI lifespan around the Retrieval v2 flow lock."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI


def _services(flows_service):
    workspace_config_service = MagicMock(hydrate_on_startup=AsyncMock())
    workspace_config_service.await_pending_mirrors = AsyncMock()
    task_service = MagicMock()
    task_service.shutdown = AsyncMock()
    return {
        "workspace_config_service": workspace_config_service,
        "flows_service": flows_service,
        "task_service": task_service,
    }


def _lifespan_app(services):
    from app.lifespan import run_shutdown, run_startup

    app = FastAPI()
    app.state.services = services
    app.state.background_tasks = set()
    app.state.mcp_lifespan_ctx = None

    @app.on_event("startup")
    async def startup():
        await run_startup(app)

    @app.on_event("shutdown")
    async def shutdown():
        await run_shutdown(app)

    return app


@pytest.fixture
def isolated_lifespan(monkeypatch):
    """Keep the real ASGI lifecycle while replacing unrelated external startup work."""
    import app.lifespan as lifespan
    import db.engine as engine
    import services.rbac_service as rbac_service
    import utils.opensearch_utils as opensearch_utils

    monkeypatch.setattr(lifespan, "UVICORN_WORKER_COUNT", 1)
    monkeypatch.setattr(lifespan, "RBAC_CACHE_BACKEND", "memory")
    monkeypatch.setattr(lifespan, "OPENRAG_BOOTSTRAP_OS_SECURITY_ON_STARTUP", False)
    monkeypatch.setattr(lifespan, "OPENRAG_ENSURE_INDEX_REPLICAS_ON_STARTUP", False)
    monkeypatch.setattr(lifespan.TelemetryClient, "send_event", AsyncMock())
    monkeypatch.setattr(lifespan, "log_bootstrap_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifespan, "startup_tasks", AsyncMock())
    monkeypatch.setattr(lifespan, "cleanup_subscriptions_proper", AsyncMock())
    monkeypatch.setattr(lifespan.clients, "cleanup", AsyncMock())
    monkeypatch.setattr(lifespan.clients, "opensearch", MagicMock())
    monkeypatch.setattr(engine, "init_engine", lambda: None)
    monkeypatch.setattr(engine, "dispose_engine", AsyncMock())
    monkeypatch.setattr(engine, "SessionLocal", None)
    monkeypatch.setattr(rbac_service, "is_rbac_enforced", lambda: False)
    monkeypatch.setattr("utils.run_mode_utils.get_run_mode", lambda: "test")
    monkeypatch.setattr(opensearch_utils, "graceful_opensearch_shutdown", AsyncMock())
    return lifespan


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["migrated", "already_migrated"])
async def test_lifespan_starts_after_verified_system_flow(isolated_lifespan, status):
    flows_service = MagicMock(
        ensure_flows_exist=AsyncMock(return_value=set()),
        migrate_persisted_retrieval_flow=AsyncMock(return_value={"status": status}),
    )
    flows_service.get_chat_flow_system_prompt = AsyncMock()
    flows_service.update_chat_flow_system_prompt = AsyncMock()
    app = _lifespan_app(_services(flows_service))

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert isolated_lifespan.startup_tasks.await_count == 1

    flows_service.ensure_flows_exist.assert_awaited_once()
    flows_service.migrate_persisted_retrieval_flow.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_allows_explicitly_configured_custom_flow_without_prompt_sync(
    isolated_lifespan,
):
    flows_service = MagicMock(
        ensure_flows_exist=AsyncMock(return_value=set()),
        migrate_persisted_retrieval_flow=AsyncMock(
            return_value={
                "status": "custom_preserved",
                "reason": "custom_flow_id",
                "flow_id": "operator-custom-flow",
            }
        ),
    )
    flows_service.get_chat_flow_system_prompt = AsyncMock()
    flows_service.update_chat_flow_system_prompt = AsyncMock()
    app = _lifespan_app(_services(flows_service))

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert isolated_lifespan.startup_tasks.await_count == 1

    flows_service.get_chat_flow_system_prompt.assert_not_awaited()
    flows_service.update_chat_flow_system_prompt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("system_migration_failed", "fetch_failed"),
        ("system_migration_failed", "backup_failed"),
        ("system_migration_failed", "unlock_failed"),
        ("system_migration_failed", "update_failed"),
        ("system_migration_failed", "verification_failed"),
        ("system_migration_failed", "update_or_verification_failed"),
        ("system_migration_failed", "lock_restore_failed"),
        ("system_migration_failed", "locked_false"),
        ("system_migration_failed", "lock_state_unknown"),
        ("system_migration_failed", "expected_graph_missing"),
        ("system_migration_failed", "thin_retrieval_tool_missing"),
        ("system_migration_failed", "wiring_invalid"),
        ("system_migration_failed", "migration_version_invalid"),
        ("system_migration_failed", "flow_missing"),
        ("system_migration_failed", "flow_id_not_configured"),
        ("skipped", "ambiguous_skip"),
    ],
)
async def test_lifespan_refuses_ready_for_every_unvalidated_system_flow(
    isolated_lifespan, status, reason
):
    flows_service = MagicMock(
        ensure_flows_exist=AsyncMock(return_value=set()),
        migrate_persisted_retrieval_flow=AsyncMock(
            return_value={
                "status": status,
                "reason": reason,
                "flow_id": "system-flow",
                "flow_state": {"known_state": "unknown", "locked": None},
            }
        ),
    )
    flows_service.get_chat_flow_system_prompt = AsyncMock()
    flows_service.update_chat_flow_system_prompt = AsyncMock()
    app = _lifespan_app(_services(flows_service))

    with pytest.raises(RuntimeError, match="system retrieval flow is not validated"):
        async with app.router.lifespan_context(app):
            pytest.fail("An unvalidated system flow must never reach ASGI ready")

    assert not app.state.background_tasks
    isolated_lifespan.startup_tasks.assert_not_awaited()
    flows_service.get_chat_flow_system_prompt.assert_not_awaited()
    flows_service.update_chat_flow_system_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_refuses_ready_when_ensuring_flows_raises(isolated_lifespan):
    flows_service = MagicMock(
        ensure_flows_exist=AsyncMock(side_effect=RuntimeError("Langflow unavailable")),
        migrate_persisted_retrieval_flow=AsyncMock(),
    )
    flows_service.get_chat_flow_system_prompt = AsyncMock()
    flows_service.update_chat_flow_system_prompt = AsyncMock()
    app = _lifespan_app(_services(flows_service))

    with pytest.raises(RuntimeError, match="flow_preparation_failed"):
        async with app.router.lifespan_context(app):
            pytest.fail("A failed critical preparation must never reach ASGI ready")

    assert not app.state.background_tasks
    flows_service.migrate_persisted_retrieval_flow.assert_not_awaited()
    flows_service.get_chat_flow_system_prompt.assert_not_awaited()
    flows_service.update_chat_flow_system_prompt.assert_not_awaited()
