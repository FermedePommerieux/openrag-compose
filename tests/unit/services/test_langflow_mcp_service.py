import pytest

import services.langflow_mcp_service as mcp_module
from services.langflow_mcp_service import LangflowMCPService


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


@pytest.mark.asyncio
async def test_update_all_mcp_server_urls_reports_failed_server_without_blocking_startup(
    monkeypatch,
):
    service = LangflowMCPService()

    async def fake_list_mcp_servers():
        return [{"name": "lf-starter_project"}]

    async def fake_patch_mcp_server_url(server_name: str):
        assert server_name == "lf-starter_project"
        return "failed"

    monkeypatch.setattr(service, "list_mcp_servers", fake_list_mcp_servers)
    monkeypatch.setattr(service, "patch_mcp_server_url", fake_patch_mcp_server_url)

    summary = await service.update_all_mcp_server_urls()

    assert summary == {"patched": 0, "skipped": 0, "failed": 1, "total": 1}


@pytest.mark.asyncio
async def test_patch_mcp_server_url_reports_failed_retryable_status_without_retry(monkeypatch):
    service = LangflowMCPService()
    monkeypatch.setenv("LANGFLOW_URL", "http://langflow:7860")

    async def fake_get_mcp_server(server_name: str):
        assert server_name == "lf-starter_project"
        return {"url": "http://localhost:8000/mcp"}

    patch_requests = []

    async def fake_langflow_request(**kwargs):
        patch_requests.append(kwargs)
        return _Response(503, "Langflow warming up")

    monkeypatch.setattr(service, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        mcp_module.clients,
        "langflow_request",
        fake_langflow_request,
        raising=True,
    )

    result = await service.patch_mcp_server_url("lf-starter_project")

    assert result == "failed"
    assert patch_requests == [
        {
            "method": "PATCH",
            "endpoint": "/api/v2/mcp/servers/lf-starter_project",
            "json": {"url": "http://langflow:7860/mcp"},
        }
    ]


@pytest.mark.asyncio
async def test_patch_mcp_server_url_only_patches_streamable_http_url(monkeypatch):
    service = LangflowMCPService()
    monkeypatch.setenv("LANGFLOW_URL", "http://langflow:7860")

    async def fake_get_mcp_server(server_name: str):
        assert server_name == "lf-starter_project"
        return {
            "url": "http://localhost:7860/api/v1/mcp/project/project-id/streamable",
            "auth_type": "api_key",
            "headers": {
                "x-api-key": "langflow-key",
                "X-Langflow-Global-Var-JWT": "JWT",
            },
        }

    patch_requests = []

    async def fake_langflow_request(**kwargs):
        patch_requests.append(kwargs)
        return _Response(200, "ok")

    monkeypatch.setattr(service, "get_mcp_server", fake_get_mcp_server)
    monkeypatch.setattr(
        mcp_module.clients,
        "langflow_request",
        fake_langflow_request,
        raising=True,
    )

    result = await service.patch_mcp_server_url("lf-starter_project")

    assert result == "patched"
    assert patch_requests == [
        {
            "method": "PATCH",
            "endpoint": "/api/v2/mcp/servers/lf-starter_project",
            "json": {"url": "http://langflow:7860/api/v1/mcp/project/project-id/streamable"},
        }
    ]
