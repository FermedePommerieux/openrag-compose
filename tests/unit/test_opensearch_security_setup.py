from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.opensearch_utils import setup_opensearch_security


@pytest.mark.asyncio
async def test_setup_opensearch_security_success():
    """Test successful security setup with all expected calls."""
    mock_client = MagicMock()
    mock_client.transport.perform_request = AsyncMock(return_value={"status": "OK", "message": "Success"})
    mock_client.cluster.health = AsyncMock(return_value={"status": "green"})

    # The current security setup uses the checked-in role documents directly;
    # it does not synthesize index patterns from get_index_name().
    roles_data = {
        "openrag_user_role": {
            "cluster_permissions": ["read"],
            "index_permissions": [{"index_patterns": ["*"], "allowed_actions": ["crud"]}]
        }
    }
    mapping_data = {
        "openrag_user_role": {"backend_roles": ["openrag_user"]},
        "all_access": {"users": ["admin"]}
    }

    # Mock file existence and content
    with (
        patch("os.path.exists", return_value=True),
        patch("builtins.open", MagicMock()),
        patch("yaml.safe_load") as mock_yaml,
    ):
        mock_yaml.side_effect = [roles_data, mapping_data]

        await setup_opensearch_security(mock_client)

        # Verify calls
        assert mock_client.transport.perform_request.call_count == 7
        mock_client.cluster.health.assert_called_once()

        # The roles document remains the authority for index patterns.
        role_put_call = mock_client.transport.perform_request.call_args_list[1]
        assert role_put_call.args[1] == "/_plugins/_security/api/roles/openrag_user_role"
        assert role_put_call.kwargs["body"] == roles_data["openrag_user_role"]


@pytest.mark.asyncio
async def test_setup_opensearch_security_graceful_auth_error():
    """Test that auth/security errors are handled gracefully without raising."""
    mock_client = MagicMock()
    # Mock a 401 Unauthorized error
    mock_client.transport.perform_request = AsyncMock(side_effect=Exception("401 Unauthorized"))
    mock_client.cluster.health = AsyncMock(return_value={"status": "green"})

    # This should NOT raise an exception
    await setup_opensearch_security(mock_client)
    assert mock_client.transport.perform_request.call_count == 2

@pytest.mark.asyncio
async def test_setup_opensearch_security_missing_files():
    """Test that missing configuration files raise FileNotFoundError."""
    mock_client = MagicMock()
    mock_client.transport.perform_request = AsyncMock()
    mock_client.cluster.health = AsyncMock(return_value={"status": "green"})

    with patch("os.path.exists", return_value=False):
        with pytest.raises(FileNotFoundError):
            await setup_opensearch_security(mock_client)
