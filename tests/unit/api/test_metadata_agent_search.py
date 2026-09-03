from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.search import metadata_agent_search
from models.metadata_agent_search import MetadataAgentQuery


@pytest.mark.asyncio
async def test_metadata_agent_endpoint_uses_existing_search_service_and_safe_diagnostics():
    service = SimpleNamespace(
        search=AsyncMock(
            return_value={
                "results": [{"chunk_id": "visible-chunk"}],
                "metadata_filter": {
                    "eligible_count": 1,
                    "visible_projection_count": 99,
                    "resolution_seconds": 0.004,
                },
            }
        )
    )
    body = MetadataAgentQuery.model_validate(
        {
            "free_text": "factures Orange",
            "filters": [
                {"field": "format_family", "operator": "EQUAL", "value": "pdf"},
                {
                    "field": "production_month",
                    "operator": "EQUAL",
                    "value": "2024-03",
                    "calendar_basis": "SOURCE_LOCAL",
                },
            ],
            "limit": 7,
        }
    )

    response = await metadata_agent_search(
        body,
        search_service=service,
        user=SimpleNamespace(user_id="user-1", jwt_token="jwt-1"),
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["metadata_agent"]["eligible_visible_occurrence_count"] == 1
    assert "visible_projection_count" not in payload["metadata_agent"]
    call = service.search.await_args
    assert call.args == ("factures Orange",)
    assert call.kwargs["user_id"] == "user-1"
    assert call.kwargs["jwt_token"] == "jwt-1"
    assert call.kwargs["limit"] == 7
    assert call.kwargs["evidence_mode"] == "scope_exhaustive"
    compiled = call.kwargs["metadata_filter"]
    assert compiled.clauses[1].calendar_basis.value == "SOURCE_LOCAL"
