"""Tests for flows_service bulk_update_flows with Langflow backup creation."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from services.flows_service import FlowsService  # noqa: E402


@pytest.mark.asyncio
async def test_bulk_update_flows_creates_backup_flow_in_langflow():
    service = FlowsService()

    mock_get_response = MagicMock()
    mock_get_response.status_code = 200
    mock_get_response.json.return_value = {
        "id": "flow-retrieval-123",
        "name": "Custom Retrieval Flow",
        "locked": False,
        "data": {"nodes": []},
    }

    mock_post_response = MagicMock()
    mock_post_response.status_code = 201
    mock_post_response.json.return_value = {
        "id": "backup-flow-999",
        "name": "Backup - Custom Retrieval Flow (2026-07-23 20:00)",
    }

    async def mock_langflow_request(method, url, json=None, **kwargs):
        if method == "GET":
            return mock_get_response
        elif method == "POST":
            # Verify the posted backup payload
            assert "id" not in json
            assert json["name"].startswith("Backup - Custom Retrieval Flow")
            assert json["locked"] is False
            return mock_post_response
        return MagicMock(status_code=404)

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=mock_langflow_request),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/backup.json"
        ),
        patch.object(
            service,
            "_reset_langflow_flow_locked",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
    ):
        results = await service.bulk_update_flows(["retrieval"], backup_custom=True)

    assert len(results) == 1
    res = results[0]
    assert res["flow_type"] == "retrieval"
    assert res["success"] is True
    assert res["backup_flow_id"] == "backup-flow-999"


@pytest.mark.asyncio
async def test_bulk_update_flows_aborts_reset_on_backup_failure():
    service = FlowsService()

    mock_get_response = MagicMock()
    mock_get_response.status_code = 200
    mock_get_response.json.return_value = {
        "id": "flow-retrieval-123",
        "name": "Custom Retrieval Flow",
        "locked": False,
        "data": {"nodes": []},
    }

    mock_post_response = MagicMock()
    mock_post_response.status_code = 500

    async def mock_langflow_request(method, url, json=None, **kwargs):
        if method == "GET":
            return mock_get_response
        elif method == "POST":
            return mock_post_response
        return MagicMock(status_code=404)

    reset_mock = AsyncMock(return_value={"success": True})

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=mock_langflow_request),
        patch.object(service, "_backup_flow", new_callable=AsyncMock, return_value=None),
        patch.object(service, "_reset_langflow_flow_locked", reset_mock),
    ):
        results = await service.bulk_update_flows(["retrieval"], backup_custom=True)

    assert len(results) == 1
    res = results[0]
    assert res["flow_type"] == "retrieval"
    assert res["success"] is False
    assert "Backup failed" in res["error"]
    assert reset_mock.call_count == 0


@pytest.mark.asyncio
async def test_bulk_update_flows_saves_local_file_backup_for_default_flows():
    service = FlowsService()

    mock_get_response = MagicMock()
    mock_get_response.status_code = 200
    mock_get_response.json.return_value = {
        "id": "flow-retrieval-123",
        "name": "Default Retrieval Flow",
        "locked": True,
        "data": {"nodes": []},
    }

    backup_mock = AsyncMock(return_value="/tmp/backup_default.json")
    reset_mock = AsyncMock(return_value={"success": True})

    with (
        patch("services.flows_service.clients.langflow_request", return_value=mock_get_response),
        patch.object(service, "_backup_flow", backup_mock),
        patch.object(service, "_reset_langflow_flow_locked", reset_mock),
    ):
        results = await service.bulk_update_flows(["retrieval"], backup_custom=True)

    assert len(results) == 1
    assert results[0]["success"] is True
    assert backup_mock.call_count == 1
    assert reset_mock.call_count == 1


@pytest.mark.asyncio
async def test_bulk_update_flows_aborts_when_preflight_get_fails():
    """Verify issue (2a): pre-flight GET failure aborts update without attempting reset."""
    service = FlowsService()

    mock_get_response = MagicMock()
    mock_get_response.status_code = 500

    reset_mock = AsyncMock(return_value={"success": True})

    with (
        patch("services.flows_service.clients.langflow_request", return_value=mock_get_response),
        patch.object(service, "_reset_langflow_flow_locked", reset_mock),
    ):
        results = await service.bulk_update_flows(["retrieval"], backup_custom=True)

    assert len(results) == 1
    res = results[0]
    assert res["flow_type"] == "retrieval"
    assert res["success"] is False
    assert "Pre-flight check failed" in res["error"]
    assert reset_mock.call_count == 0


def test_per_user_dismissal_isolation():
    """Verify issue (2c): dismissal for user A does not dismiss for user B."""
    service = FlowsService()

    service.dismiss_flows_updates(["retrieval"], user_id="user_A")

    assert "user_A" in service._dismissed_updates
    assert "retrieval" in service._dismissed_updates["user_A"]
    assert (
        "user_B" not in service._dismissed_updates
        or "retrieval" not in service._dismissed_updates.get("user_B", set())
    )


@pytest.mark.asyncio
async def test_bulk_update_flows_dismissal_cleared_only_on_success():
    """Verify issue (4c): dismissal state is preserved on failure and cleared on success."""
    service = FlowsService()

    # Pre-dismiss update for user_A
    service.dismiss_flows_updates(["retrieval"], user_id="user_A")
    assert "retrieval" in service._dismissed_updates["user_A"]

    mock_get_fail = MagicMock(status_code=500)

    # 1. Update fails (pre-flight GET returns 500) -> dismissal remains intact
    with patch("services.flows_service.clients.langflow_request", return_value=mock_get_fail):
        results = await service.bulk_update_flows(["retrieval"], backup_custom=True)

    assert results[0]["success"] is False
    assert "retrieval" in service._dismissed_updates.get("user_A", set())

    # 2. Update succeeds -> dismissal is cleared
    mock_get_ok = MagicMock(status_code=200)
    mock_get_ok.json.return_value = {
        "id": "flow-retrieval-123",
        "name": "Retrieval Flow",
        "locked": True,
        "data": {"nodes": []},
    }

    with (
        patch("services.flows_service.clients.langflow_request", return_value=mock_get_ok),
        patch.object(service, "_backup_flow", AsyncMock(return_value="/tmp/backup.json")),
        patch.object(
            service, "_reset_langflow_flow_locked", AsyncMock(return_value={"success": True})
        ),
    ):
        results = await service.bulk_update_flows(["retrieval"], backup_custom=True)

    assert results[0]["success"] is True
    assert "retrieval" not in service._dismissed_updates.get("user_A", set())


@pytest.mark.asyncio
async def test_ensure_flows_exist_does_not_auto_update_on_startup():
    """Verify that existing flows are skipped on startup and never auto-updated."""
    service = FlowsService()

    mock_get_ok = MagicMock(status_code=200)
    mock_get_ok.json.return_value = {
        "id": "flow-retrieval-123",
        "name": "Retrieval Flow",
        "locked": True,
        "updated_at": "2020-01-01T00:00:00Z",
    }

    reset_mock = AsyncMock()
    backup_mock = AsyncMock()

    with (
        patch("services.flows_service.clients.langflow_request", return_value=mock_get_ok),
        patch.object(service, "_backup_flow", backup_mock),
        patch.object(service, "_reset_langflow_flow_locked", reset_mock),
    ):
        created = await service.ensure_flows_exist()

    assert created == set()
    assert backup_mock.call_count == 0
    assert reset_mock.call_count == 0


@pytest.mark.asyncio
async def test_ensure_flows_exist_reapplies_system_settings_after_system_flow_creation(monkeypatch):
    """The custom-flow safeguard must not alter default system-flow recovery."""
    import api.settings
    import services.flows_service as flows_module

    service = FlowsService()
    missing_flow_id = flows_module.NUDGES_FLOW_ID
    created_flow_ids: set[str] = set()

    async def langflow_request(method, url, json=None, **_kwargs):
        flow_id = url.rsplit("/", 1)[-1]
        if method == "GET" and flow_id == missing_flow_id and flow_id not in created_flow_ids:
            return MagicMock(status_code=404)
        if method == "GET":
            return MagicMock(status_code=200)
        if method == "PUT" and flow_id == missing_flow_id:
            created_flow_ids.add(flow_id)
            return MagicMock(status_code=201)
        raise AssertionError(f"unexpected Langflow request: {method} {url}")

    reapply = AsyncMock()
    monkeypatch.setattr(flows_module.clients, "langflow_request", langflow_request)
    monkeypatch.setattr(
        flows_module,
        "get_openrag_config",
        lambda: MagicMock(edited=True),
    )
    monkeypatch.setattr(api.settings, "reapply_all_settings", reapply)

    created = await service.ensure_flows_exist()

    assert created == {"nudges"}
    assert created_flow_ids == {missing_flow_id}
    reapply.assert_awaited_once()


def _lifecycle_retrieval_flow() -> dict:
    """Load the exact production lifecycle baseline, not an approximate graph."""
    raw = subprocess.check_output(
        ["git", "show", "156f3664fd2b8d4f4ad20248321a313a7034fc9b:flows/openrag_agent.json"],
        cwd=ROOT,
        text=True,
    )
    return json.loads(raw)


def _unversioned_retrieval_v2_flow() -> dict:
    """Load the GitOps-bootstrap state that precedes the runtime marker."""
    return json.loads((ROOT / "flows" / "openrag_agent.json").read_text())


def _unversioned_retrieval_v17_flow() -> dict:
    """Load the exact versionless graph deployed by GitOps as v2.42."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "6a77b031e583885287eea38d6e99189d3f71a156:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    return json.loads(raw)


def _versioned_retrieval_v5_flow() -> dict:
    """Load the exact deployed repository graph and add its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "b8a8203f2c2e506bba1670f3364570876d6aa69b:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 5
    return flow


def _versioned_retrieval_v6_flow() -> dict:
    """Load the exact documentalist graph deployed before forced execution."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "d17631c921c2f575c4919a7e2697245b8049f6a6:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 6
    return flow


def _versioned_retrieval_v7_flow() -> dict:
    """Load the deployed graph whose search context was not request-bound."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "f4d7587f7447dc21bff7b6b25e39311285106349:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 7
    return flow


def _versioned_retrieval_v8_flow(*, langflow_normalized: bool = False) -> dict:
    """Recreate both exact v8 graphs observed before the StrInput repair.

    Langflow persisted the bundled graph first, then rebuilt the MultilineInput
    component from its older source and cleared ``load_from_db``. Both states
    have production fingerprints; arbitrary v8 graphs must still fail closed.
    """
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "eee123a375e0472262b6506b7e46d0703323ea6c:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 8
    if not langflow_normalized:
        return flow

    component_code = subprocess.check_output(
        [
            "git",
            "show",
            "f4d7587f7447dc21bff7b6b25e39311285106349:custom_components/openrag/backend_retrieval.py",
        ],
        cwd=ROOT,
        text=True,
    )
    retrieval_node = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    template = retrieval_node["data"]["node"]["template"]
    template["code"]["value"] = component_code
    template["filter_expression"]["load_from_db"] = False
    return flow


def _versioned_retrieval_v9_flow(*, stale_shared_source: bool = False) -> dict:
    """Load intended v9 or the exact stale shared-flow state seen in production."""
    revision = (
        "2406010410076ff5f18bf4dc6a6c79e05fc9585f"
        if stale_shared_source
        else "f841d2c90624e311a107b4c549c2412defa27e2d"
    )
    raw = subprocess.check_output(
        ["git", "show", f"{revision}:flows/openrag_agent.json"],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 9
    return flow


def _versioned_retrieval_v10_flow() -> dict:
    """Load the exact graph that still delegated continuation to the model."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "3506fc2a03117b0eecbeb7136a2f23f0294102a8:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 10
    return flow


def _versioned_retrieval_v11_flow() -> dict:
    """Load the exact graph that repeated full provenance in model context."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "e3c713a0290f7919f059893c823277004b079386:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 11
    return flow


def _versioned_retrieval_v12_flow() -> dict:
    """Load the exact compact graph before deep archive-audit discovery."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "6858763b44f025f583aa2c310016777ebf9ca24b:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 12
    return flow


def _versioned_retrieval_v13_flow() -> dict:
    """Load the deployed deep-audit graph before contextual PROV-O review."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "1a5793639e58ff67daa611271e8394afd7feb4a5:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 13
    return flow


def _versioned_retrieval_v15_flow() -> dict:
    """Load the deployed graph before GPT-5.6 tools moved to Responses."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "056ad14bdba532a5564332f13e3d2c4469a549fa:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 15
    return flow


def _versioned_retrieval_v16_flow() -> dict:
    """Load the deployed Responses graph before adaptive expansion gating."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "abc6f518:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 16
    return flow


def _previous_bundled_retrieval_flow() -> dict:
    """Load the pre-documentalist bundled graph for historical migrations."""
    flow = _versioned_retrieval_v5_flow()
    flow["data"].pop("openrag_retrieval_version")
    return flow


def _flow_response(status_code: int, payload: dict | None = None) -> MagicMock:
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    return response


class _RetrievalMigrationTransport:
    """Small stateful Langflow double for lock-transition fault injection."""

    def __init__(self, failure: str | None = None):
        self.failure = failure
        self.flow = _lifecycle_retrieval_flow()
        self.unlocked = False
        self.put_completed = False
        self.relock_attempted = False
        self.relock_verified = False

    async def __call__(self, method, url, json=None, **kwargs):
        if method == "GET":
            if self.failure == "disappear" and self.unlocked:
                return _flow_response(404)
            if self.failure == "verify_put" and self.put_completed:
                invalid_flow = copy.deepcopy(self.flow)
                invalid_flow["data"]["nodes"][0]["data"]["node"]["description"] = "invalid"
                return _flow_response(200, invalid_flow)
            if (
                self.failure == "verify_relock"
                and self.relock_attempted
                and not self.relock_verified
            ):
                self.relock_verified = True
                self.flow["locked"] = False
            return _flow_response(200, self.flow)
        if method == "PATCH":
            if json == {"locked": False}:
                if self.failure == "unlock":
                    return _flow_response(500)
                self.flow["locked"] = False
                self.unlocked = True
                return _flow_response(200, self.flow)
            if json == {"locked": True}:
                self.relock_attempted = True
                if self.failure == "relock" or (self.failure == "disappear" and self.unlocked):
                    return _flow_response(500)
                self.flow["locked"] = True
                return _flow_response(200, self.flow)
        if method == "PUT":
            if self.failure == "put":
                return _flow_response(500)
            self.flow = json
            self.put_completed = True
            return _flow_response(200, self.flow)
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.mark.asyncio
async def test_migrate_known_lifecycle_retrieval_flow_is_backed_up_and_idempotent():
    service = FlowsService()
    old_flow = _lifecycle_retrieval_flow()
    migrated_flow: dict | None = None
    calls: list[tuple[str, dict | None]] = []

    async def mock_langflow_request(method, url, json=None, **kwargs):
        nonlocal migrated_flow
        calls.append((method, json))
        if method == "GET":
            response = MagicMock(status_code=200)
            response.json.return_value = migrated_flow or old_flow
            return response
        if method == "PATCH":
            target = migrated_flow or old_flow
            target["locked"] = json["locked"]
            response = MagicMock(status_code=200)
            response.json.return_value = target
            return response
        if method == "PUT":
            migrated_flow = json
            return MagicMock(status_code=200)
        raise AssertionError(f"unexpected request {method} {url}")

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=mock_langflow_request),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ) as backup,
    ):
        first = await service.migrate_persisted_retrieval_flow()
        second = await service.migrate_persisted_retrieval_flow()

    assert first["status"] == "migrated"
    assert first["backup_path"] == "/tmp/flow.json"
    assert second["status"] == "already_migrated"
    assert backup.await_count == 1
    component_types = [
        node.get("data", {}).get("node", {}).get("namespaced_id")
        for node in migrated_flow["data"]["nodes"]
    ]
    assert "ext:openrag:OpenRAGBackendRetrievalComponent@extra" in component_types
    assert (
        "ext:openrag:OpenSearchVectorStoreComponentMultimodalMultiEmbedding@extra"
        not in component_types
    )
    assert [payload for method, payload in calls if method == "PATCH"] == [
        {"locked": False},
        {"locked": True},
    ]


@pytest.mark.asyncio
async def test_migrate_unversioned_retrieval_v2_flow_synchronized_by_gitops():
    """The exact pinned flow must receive its marker rather than fail closed."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _unversioned_retrieval_v2_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ) as backup,
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert result["backup_path"] == "/tmp/flow.json"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    assert (
        "exhaustive digital documentalist"
        in agent_node["data"]["node"]["template"]["system_prompt"]["value"]
    )
    assert backup.await_count == 1


@pytest.mark.asyncio
async def test_migrate_exact_unversioned_v17_graph_synchronized_by_gitops():
    """The production v2.42 source graph upgrades without weakening fail-closed checks."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _unversioned_retrieval_v17_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v5_graph_to_current_documentalist():
    """Repository-owned v5 upgrades without authorizing any edited graph."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v5_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v6_graph_to_forced_exhaustive_execution():
    """The production v6 graph upgrades, while edited graphs remain protected."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v6_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    assert agent_node["data"]["node"]["template"]["max_iterations"]["value"] == 128
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v7_graph_to_request_bound_context():
    """The production v7 graph receives the corrected Langflow input binding."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v7_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    assert retrieval_node["data"]["node"]["template"]["filter_expression"]["load_from_db"] is True
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("langflow_normalized", [False, True])
async def test_migrate_exact_deployed_v8_graph_to_stable_request_binding(
    langflow_normalized: bool,
):
    """Both known v8 states receive the StrInput binding without widening authority."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v8_flow(langflow_normalized=langflow_normalized)

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    context_field = retrieval_node["data"]["node"]["template"]["filter_expression"]
    assert context_field["_input_type"] == "StrInput"
    assert context_field["input_types"] == []
    assert context_field["multiline"] is False
    assert context_field["load_from_db"] is True
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_shared_source", [False, True])
async def test_migrate_exact_deployed_v9_graph_after_shared_source_alignment(
    stale_shared_source: bool,
):
    """Version 10 repairs only the two known v9 states after GitOps alignment."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v9_flow(stale_shared_source=stale_shared_source)

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    context_field = retrieval_node["data"]["node"]["template"]["filter_expression"]
    assert context_field["_input_type"] == "StrInput"
    assert context_field["load_from_db"] is True
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v10_graph_to_explicit_document_cursor_api():
    """The known v10 graph receives the explicit single-document read API."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v10_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval_node["data"]["node"]["template"]["code"]["value"]
    assert 'if mode == "exhaustive" and not resolved_document_id' in code
    assert '"evidenceMode": backend_mode' in code
    assert "seen_cursors" not in code
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v11_graph_to_compact_model_evidence():
    """The known v11 graph keeps full artifacts but projects model content."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v11_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval_node["data"]["node"]["template"]["code"]["value"]
    assert "def _model_payload" in code
    assert "json.dumps(_model_payload(payload)" in code
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v12_graph_to_normal_provenance_search():
    """The known v12 graph loses the retired archive-audit switch."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v12_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval_node["data"]["node"]["template"]["code"]["value"]
    assert "backend_mode = mode" in code
    assert 'backend_mode = "audit" if' not in code
    assert '"archive_audit_candidates"' not in code
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v13_graph_to_normal_provenance_retrieval():
    """The exact production v13 graph receives the current normal retrieval tool."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v13_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval_node["data"]["node"]["template"]["code"]["value"]
    assert "AUDIT_BACKEND_TIMEOUT_SECONDS" not in code
    assert '"retrieval_relation_paths"' in code
    assert '"document_graph"' in code
    assert '"noise_accounting"' in code
    assert '"contextual_review"' not in code
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v15_graph_to_responses_transport():
    """The production v15 Agent must retain tools and reasoning on Responses."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v15_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    code = agent_node["data"]["node"]["template"]["code"]["value"]
    assert 'overrides["use_responses_api"] = True' in code
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v16_graph_to_normal_provenance_retrieval():
    """The production v16 graph receives the current normal retrieval tool."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v16_flow()

    with (
        patch(
            "services.flows_service.clients.langflow_request",
            side_effect=transport.__call__,
        ),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18
    retrieval_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval_node["data"]["node"]["template"]["code"]["value"]
    assert "AUDIT_BACKEND_TIMEOUT_SECONDS" not in code
    assert '"progressId"' in code
    assert '"expansion_selectivity"' not in code
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
async def test_migrate_first_versioned_retrieval_v2_prompt_revision():
    """The exact prior v3 graph may receive the document-wide evidence rule."""
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _previous_bundled_retrieval_flow()
    transport.flow["data"]["openrag_retrieval_version"] = 3

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    assert (
        "exhaustive digital documentalist"
        in agent_node["data"]["node"]["template"]["system_prompt"]["value"]
    )


@pytest.mark.asyncio
async def test_migrate_role_evidence_prompt_revision():
    """The exact prior role-evidence graph may receive the document-reference rule."""
    from config.config_manager import LEGACY_SYSTEM_PROMPTS

    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _previous_bundled_retrieval_flow()
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent_node["data"]["node"]["template"]["system_prompt"]["value"] = LEGACY_SYSTEM_PROMPTS[-3]
    transport.flow["data"]["openrag_retrieval_version"] = 3

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18


@pytest.mark.asyncio
async def test_migrate_evidence_first_prompt_revision():
    """The exact prior evidence-first graph may receive relationship attribution."""
    from config.config_manager import LEGACY_SYSTEM_PROMPTS

    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _previous_bundled_retrieval_flow()
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent_node["data"]["node"]["template"]["system_prompt"]["value"] = LEGACY_SYSTEM_PROMPTS[-2]
    transport.flow["data"]["openrag_retrieval_version"] = 4

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service,
            "_backup_flow",
            new_callable=AsyncMock,
            return_value="/tmp/flow.json",
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["data"]["openrag_retrieval_version"] == 18


@pytest.mark.asyncio
async def test_migrate_retrieval_flow_fails_closed_for_an_altered_system_graph():
    service = FlowsService()
    custom_flow = _lifecycle_retrieval_flow()
    custom_flow["data"]["nodes"][0]["data"]["node"]["description"] = "operator customization"
    response = MagicMock(status_code=200)
    response.json.return_value = custom_flow

    with (
        patch("services.flows_service.clients.langflow_request", return_value=response),
        patch.object(service, "_backup_flow", new_callable=AsyncMock) as backup,
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == "system_flow_unverified"
    backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_migrate_retrieval_flow_fails_closed_for_an_unlocked_system_graph():
    service = FlowsService()
    custom_flow = _lifecycle_retrieval_flow()
    custom_flow["locked"] = False
    response = MagicMock(status_code=200)
    response.json.return_value = custom_flow

    with (
        patch("services.flows_service.clients.langflow_request", return_value=response),
        patch.object(service, "_backup_flow", new_callable=AsyncMock) as backup,
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == "system_flow_unverified"
    backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_migrate_retrieval_flow_preserves_an_explicitly_configured_custom_flow(monkeypatch):
    import services.flows_service as flows_module

    monkeypatch.setattr(flows_module, "LANGFLOW_CHAT_FLOW_ID", "operator-custom-flow")
    service = FlowsService()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "id": "operator-custom-flow",
        "name": "Operator retrieval flow",
        "locked": False,
        "data": {"nodes": []},
    }

    with (
        patch("services.flows_service.clients.langflow_request", return_value=response),
        patch.object(service, "_backup_flow", new_callable=AsyncMock) as backup,
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result == {
        "status": "custom_preserved",
        "reason": "custom_flow_id",
        "flow_id": "operator-custom-flow",
    }
    backup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "reason"),
    [(404, "flow_missing"), (500, "fetch_failed")],
)
async def test_migrate_retrieval_flow_classifies_unavailable_system_flow_as_failure(
    http_status, reason
):
    service = FlowsService()
    response = MagicMock(status_code=http_status)

    with patch("services.flows_service.clients.langflow_request", return_value=response):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == reason


@pytest.mark.asyncio
async def test_migrate_retrieval_flow_stops_before_unlock_when_backup_fails():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(service, "_backup_flow", new_callable=AsyncMock, return_value=None),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == "backup_failed"
    assert transport.flow["locked"] is True
    assert transport.unlocked is False


@pytest.mark.asyncio
async def test_migrate_retrieval_flow_reports_unlock_failure_with_locked_state():
    service = FlowsService()
    transport = _RetrievalMigrationTransport("unlock")

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == "unlock_failed"
    assert result["flow_state"] == {"known_state": "locked", "locked": True}
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["put", "verify_put", "verify_relock"])
async def test_migrate_retrieval_flow_restores_lock_after_safe_transition_failures(failure):
    service = FlowsService()
    transport = _RetrievalMigrationTransport(failure)

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == "update_or_verification_failed"
    assert result["flow_state"] == {"known_state": "locked", "locked": True}
    assert transport.flow["locked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["relock", "disappear"])
async def test_migrate_retrieval_flow_fails_closed_when_lock_cannot_be_restored(failure):
    service = FlowsService()
    transport = _RetrievalMigrationTransport(failure)

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "system_migration_failed"
    assert result["reason"] == "lock_restore_failed"
    assert result["error"]
    assert result["lock_error"]
    assert result["flow_id"]
    assert result["version"] == 18
    assert result["flow_state"]["known_state"] in {"unlocked", "missing"}


@pytest.mark.asyncio
async def test_get_flows_updates_available_includes_all_flows():
    """Verify that get_flows_updates_available surfaces updates regardless of locked status."""
    service = FlowsService()

    mock_update_info = {
        "flow_type": "retrieval",
        "flow_id": "flow-retrieval-123",
        "is_custom": False,
    }

    with patch.object(service, "_check_flow_update", AsyncMock(return_value=mock_update_info)):
        updates = await service.get_flows_updates_available()

    assert len(updates) == 4
    assert all(u["flow_type"] in ["nudges", "retrieval", "ingest", "url_ingest"] for u in updates)


@pytest.mark.asyncio
async def test_check_flow_update_reports_installed_source_provenance(tmp_path, monkeypatch):
    """The prompt identifies the installed flow source, never Langflow upstream."""
    service = FlowsService()
    flow_file = tmp_path / "openrag_agent.json"
    flow_file.write_text("{}")
    monkeypatch.setenv("OPENRAG_FLOWS_SOURCE_REPOSITORY", "FermedePommerieux/openrag-compose")
    monkeypatch.setenv("OPENRAG_FLOWS_SOURCE_BRANCH", "pommerieux/v0.6.0-retrieval-v2")
    monkeypatch.setenv(
        "OPENRAG_FLOWS_SOURCE_REVISION",
        "92a40e9922e12fd7aa06b53bf841b061b41d4818",
    )

    with patch.object(service, "_find_flow_file_by_id", return_value=str(flow_file)):
        update = await service._check_flow_update(
            "retrieval",
            "flow-retrieval-123",
            {"updated_at": "1970-01-01T00:00:00Z", "locked": True},
        )

    assert update is not None
    assert update["source"]["repository"] == "FermedePommerieux/openrag-compose"
    assert update["source"]["branch"] == "pommerieux/v0.6.0-retrieval-v2"
    assert update["source"]["revision"].startswith("92a40e99")
