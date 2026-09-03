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
    """Load the exact pre-contract v6 graph and add its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "c4d3c7395863c157c3be17c8eaa03fa9b3e90f06:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 6
    return flow


def _versioned_retrieval_v7_flow() -> dict:
    """Load the exact focused-only graph preceding scope investigation."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "28332fd043522cdb43224db0b0f8a856c68f5a51:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 7
    return flow


def _versioned_retrieval_v8_flow() -> dict:
    """Load the exact frozen Phase 1 graph and add its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "ded5317aaf7497c2161d4edfc655149075686e8e:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 8
    return flow


def _versioned_retrieval_v9_flow() -> dict:
    """Load the exact runtime-safe graph preceding policy-certificate projection."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "be5c1077ae4c4f705c634ba945f2b4b0d8fd5dfc:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 9
    return flow


def _versioned_retrieval_v10_flow() -> dict:
    """Load the frozen Phase 2 scope-policy graph with its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "477092776baaacfc9fb6131766e83b32f60b181d:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 10
    return flow


def _versioned_retrieval_v11_flow() -> dict:
    """Load the deterministic agent-guard graph with its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "ce63f30b1bb0455f651a6aad9ff8a28eb77f87fd:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 11
    return flow


def _versioned_retrieval_v12_flow() -> dict:
    """Load the bounded multi-query graph with its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "306d7f1cbad47bf5d81399c24a88f612c8417e94:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 12
    return flow


def _versioned_retrieval_v13_flow() -> dict:
    """Load the deployed fail-closed graph with its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "74263a9b812a2eb0a83bc676cd37d3dfe82c0e1a:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 13
    return flow


def _versioned_retrieval_v14_flow() -> dict:
    """Load the production metadata-side-index baseline with its runtime marker."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "22886b9121a0d7cc9cea7e0ee04e204b0ae5ff23:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 14
    return flow


def _previous_versioned_retrieval_v15_flow() -> dict:
    """Load v15 before the dynamic Pydantic namespace repair."""
    raw = subprocess.check_output(
        [
            "git",
            "show",
            "547b791bc459290986ca33b827f0c449940c398c:flows/openrag_agent.json",
        ],
        cwd=ROOT,
        text=True,
    )
    flow = json.loads(raw)
    flow["data"]["openrag_retrieval_version"] = 15
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
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    agent_node = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    assert (
        "three evidence paths" in agent_node["data"]["node"]["template"]["system_prompt"]["value"]
    )
    assert backup.await_count == 1


@pytest.mark.asyncio
async def test_migrate_exact_deployed_v5_graph_to_scope_exhaustive_v8():
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
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    assert transport.flow["locked"] is True


def test_exact_repository_owned_version_6_graph_is_migration_eligible():
    """Settings fields may vary; prompt and code changes remain protected."""
    service = FlowsService()
    flow = _versioned_retrieval_v6_flow()
    embedding = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Embedding Model"
    )
    embedding_template = embedding["data"]["node"]["template"]
    embedding_template["api_key"]["value"] = "OPENAI_API_KEY"
    embedding_template["api_key"]["load_from_db"] = True
    embedding_template["model"]["value"] = [
        {"name": "text-embedding-3-large", "provider": "OpenAI"}
    ]
    embedding_template["model"]["options"] = embedding_template["model"]["value"]

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    embedding_template["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


def test_migrated_graph_accepts_runtime_managed_prompt_but_rejects_code_edits():
    service = FlowsService()
    flow = _unversioned_retrieval_v2_flow()
    flow["data"]["openrag_retrieval_version"] = 15
    language_model = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Language Model"
    )
    language_template = language_model["data"]["node"]["template"]
    language_template["model"]["value"] = [{"name": "gpt-5.6-sol", "provider": "OpenAI"}]
    language_template["model"]["options"] = language_template["model"]["value"]
    assert service._is_known_migrated_retrieval_flow(flow) is True

    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent["data"]["node"]["template"]["system_prompt"]["value"] += " custom"
    assert service._is_known_migrated_retrieval_flow(flow) is True
    agent["data"]["node"]["template"]["code"]["value"] += "\n# unauthorized"
    assert service._is_known_migrated_retrieval_flow(flow) is False


@pytest.mark.asyncio
async def test_agent_external_model_input_is_updated_without_serialized_options():
    service = FlowsService()
    flow = _unversioned_retrieval_v2_flow()
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    model_field = agent["data"]["node"]["template"]["model"]
    assert "options" not in model_field

    with (
        patch.object(service, "_enable_model_in_langflow", new_callable=AsyncMock),
        patch.object(
            service,
            "_update_component_langflow",
            new_callable=AsyncMock,
            side_effect=lambda template, _selection: template,
        ),
    ):
        updated = await service._update_component_fields(agent, "openai", "gpt-5.4-mini")

    assert updated is True
    assert service._runtime_model_identity(model_field["value"]) == (
        "openai",
        "gpt-5.4-mini",
    )


def test_version_6_migration_preserves_settings_managed_model_values():
    service = FlowsService()
    flow = _versioned_retrieval_v6_flow()
    embedding = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Embedding Model"
    )
    embedding_template = embedding["data"]["node"]["template"]
    embedding_template["api_key"]["value"] = "OPENAI_API_KEY"
    embedding_template["api_key"]["load_from_db"] = True
    embedding_template["model"]["value"] = [
        {"name": "text-embedding-3-large", "provider": "OpenAI"}
    ]

    migrated = service._migrate_known_legacy_retrieval_flow(flow)

    assert migrated is not None
    migrated_embedding = next(
        node
        for node in migrated["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Embedding Model"
    )
    migrated_template = migrated_embedding["data"]["node"]["template"]
    assert migrated_template["api_key"]["value"] == "OPENAI_API_KEY"
    assert migrated_template["api_key"]["load_from_db"] is True
    assert migrated_template["model"]["value"][0]["name"] == "text-embedding-3-large"
    assert migrated["data"]["openrag_retrieval_version"] == 15


def test_exact_repository_owned_version_7_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v7_flow()

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent["data"]["node"]["template"]["system_prompt"]["value"] += " custom"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_version_7_graph_to_scope_exhaustive_v10():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v7_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15


def test_exact_phase1_version_8_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v8_flow()

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_phase1_version_8_to_artifact_boundary_v10():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v8_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    assert '"responseProfile": "langflow"' in retrieval["data"]["node"]["template"]["code"]["value"]


def test_exact_runtime_safe_version_9_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v9_flow()
    language_model = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Language Model"
    )
    language_model["data"]["node"]["template"]["model"]["value"] = [
        {"name": "gpt-5.6-sol", "provider": "OpenAI"}
    ]

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_runtime_safe_version_9_to_policy_certificate_v10():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v9_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval["data"]["node"]["template"]["code"]["value"]
    assert '"scope_policy_id"' in code
    assert '"scope_context_relations"' in code


def test_exact_phase2_version_10_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v10_flow()
    language_model = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Language Model"
    )
    language_model["data"]["node"]["template"]["model"]["value"] = [
        {"name": "gpt-5.6-sol", "provider": "OpenAI"}
    ]

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent["data"]["node"]["template"]["system_prompt"]["value"] += " custom"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_phase2_version_10_to_current_retrieval_v12():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v10_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    agent = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    template = agent["data"]["node"]["template"]
    assert "OpenRAGRetrievalGuardMiddleware" in template["code"]["value"]
    assert "never repeat equivalent exhaustive discovery" in template["system_prompt"]["value"]


def test_exact_agent_guard_version_11_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v11_flow()
    language_model = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Language Model"
    )
    language_model["data"]["node"]["template"]["model"]["value"] = [
        {"name": "gpt-5.6-sol", "provider": "OpenAI"}
    ]

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_agent_guard_version_11_to_multi_query_v12():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v11_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval["data"]["node"]["template"]["code"]["value"]
    assert "multi_query_discovery" in code


def test_exact_multi_query_version_12_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v12_flow()

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_multi_query_version_12_to_current_version_14():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v12_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    code = retrieval["data"]["node"]["template"]["code"]["value"]
    assert '"warnings"' in code
    assert '"retrieval_execution_complete"' in code


def test_exact_fail_closed_version_13_graph_is_migration_eligible():
    service = FlowsService()
    flow = _versioned_retrieval_v13_flow()
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent["data"]["node"]["template"]["system_prompt"]["value"] = "configured live prompt"

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_fail_closed_version_13_to_audit_closeout_version_14():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v13_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    agent = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    assert "_uses_canonical_coverage_certificate" in agent["data"]["node"]["template"]["code"]["value"]
    assert "_without_opaque_relation_targets" in retrieval["data"]["node"]["template"]["code"]["value"]


def test_exact_version_14_graph_is_eligible_only_without_operator_changes():
    service = FlowsService()
    flow = _versioned_retrieval_v14_flow()
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent["data"]["node"]["template"]["system_prompt"]["value"] = "configured live prompt"

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


def test_premature_version_15_marker_is_eligible_only_on_exact_version_14_graph():
    service = FlowsService()
    flow = _versioned_retrieval_v14_flow()
    flow["data"]["openrag_retrieval_version"] = 15
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
    )
    agent["data"]["node"]["template"]["system_prompt"]["value"] = "configured live prompt"

    assert service._is_known_previous_retrieval_v2_flow(flow) is True

    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    retrieval["data"]["node"]["template"]["code"]["value"] += "\n# operator customization"
    assert service._is_known_previous_retrieval_v2_flow(flow) is False


@pytest.mark.asyncio
async def test_migrate_exact_version_14_to_bounded_metadata_tool_version_15():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v14_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    assert transport.flow["data"]["openrag_retrieval_version"] == 15
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    assert {output["name"] for output in retrieval["data"]["node"]["outputs"]} == {
        "component_as_tool",
        "metadata_search_tool",
    }
    assert "document_search_with_metadata" in retrieval["data"]["node"]["template"]["code"]["value"]


@pytest.mark.asyncio
async def test_recover_premature_version_15_marker_on_exact_version_14_graph():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _versioned_retrieval_v14_flow()
    transport.flow["data"]["openrag_retrieval_version"] = 15

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    assert {output["name"] for output in retrieval["data"]["node"]["outputs"]} == {
        "component_as_tool",
        "metadata_search_tool",
    }


@pytest.mark.asyncio
async def test_migrate_exact_version_15_dynamic_schema_repair():
    service = FlowsService()
    transport = _RetrievalMigrationTransport()
    transport.flow = _previous_versioned_retrieval_v15_flow()

    with (
        patch("services.flows_service.clients.langflow_request", side_effect=transport.__call__),
        patch.object(
            service, "_backup_flow", new_callable=AsyncMock, return_value="/tmp/flow.json"
        ),
    ):
        result = await service.migrate_persisted_retrieval_flow()

    assert result["status"] == "migrated"
    assert transport.flow["locked"] is True
    retrieval = next(
        node
        for node in transport.flow["data"]["nodes"]
        if node.get("data", {}).get("node", {}).get("display_name") == "OpenRAG Retrieval v2"
    )
    assert '"MetadataToolField": MetadataToolField' in (
        retrieval["data"]["node"]["template"]["code"]["value"]
    )


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
        "three evidence paths" in agent_node["data"]["node"]["template"]["system_prompt"]["value"]
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
    assert transport.flow["data"]["openrag_retrieval_version"] == 15


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
    assert transport.flow["data"]["openrag_retrieval_version"] == 15


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
    assert result["version"] == 15
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
