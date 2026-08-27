from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.documents import _ensure_index_exists


@pytest.mark.asyncio
async def test_ingestion_index_check_does_not_reconfigure_security(monkeypatch):
    admin_client = object()
    factory = MagicMock(return_value=admin_client)
    monkeypatch.setattr(
        "config.settings.clients",
        SimpleNamespace(create_index_admin_opensearch_client=factory),
    )
    init_index = AsyncMock()

    with patch("main.init_index", init_index):
        await _ensure_index_exists("jwt")

    init_index.assert_awaited_once_with(
        admin_client,
        configure_security=False,
    )
