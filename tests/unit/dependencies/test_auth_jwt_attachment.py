import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import auth.request_identity as request_identity  # noqa: E402
from dependencies import get_current_user, get_optional_user  # noqa: E402
from session_manager import User  # noqa: E402


class FakeSessionManager:
    def __init__(self):
        self.users: dict[str, User] = {}
        self.effective_token_calls: list[tuple[str, str | None]] = []

    def verify_token(self, token):
        return {}  # Legacy Google session without a durable-session claim.

    def get_effective_jwt_token(self, user_id: str, jwt_token: str | None) -> str:
        self.effective_token_calls.append((user_id, jwt_token))
        return jwt_token or "Bearer generated-anonymous-token"

    def get_user_from_token(self, token: str) -> User | None:
        return User(
            user_id="user-1",
            email="user@example.com",
            name="Test User",
            provider="google",
        )


def _request(cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        cookies=cookies or {},
        headers=headers or {},
        state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace(services={})),
    )


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch):
    monkeypatch.setattr("config.settings.IBM_AUTH_ENABLED", False)

    async def _resolve_db_user_id(user, jwt_roles=None):
        return user.user_id

    monkeypatch.setattr(request_identity, "_resolve_db_user_id", _resolve_db_user_id)


@pytest.mark.asyncio
async def test_current_user_no_auth_gets_effective_jwt(monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    session_manager = FakeSessionManager()
    request = _request()

    user = await get_current_user(request, session_manager=session_manager)

    assert user.user_id == "anonymous"
    assert user.jwt_token == "Bearer generated-anonymous-token"
    assert request.state.user.jwt_token == "Bearer generated-anonymous-token"
    assert session_manager.effective_token_calls == [("anonymous", None)]


@pytest.mark.asyncio
async def test_optional_user_no_auth_gets_effective_jwt(monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    session_manager = FakeSessionManager()
    request = _request()

    user = await get_optional_user(request, session_manager=session_manager)

    assert user is not None
    assert user.user_id == "anonymous"
    assert user.jwt_token == "Bearer generated-anonymous-token"
    assert request.state.user.jwt_token == "Bearer generated-anonymous-token"
    assert session_manager.effective_token_calls == [("anonymous", None)]


@pytest.mark.asyncio
async def test_current_user_cookie_token_is_attached_for_opensearch(monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: False)
    session_manager = FakeSessionManager()
    request = _request(cookies={"auth_token": "Bearer session-token"})

    user = await get_current_user(request, session_manager=session_manager)

    assert user.user_id == "user-1"
    assert user.jwt_token == "Bearer session-token"
    assert request.state.user.jwt_token == "Bearer session-token"
    assert session_manager.effective_token_calls == [("user-1", "Bearer session-token")]


@pytest.mark.asyncio
async def test_current_user_bearer_token_is_attached_for_langflow_retrieval(monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: False)
    session_manager = FakeSessionManager()
    request = _request(headers={"Authorization": "Bearer user-token"})

    user = await get_current_user(request, session_manager=session_manager)

    assert user.user_id == "user-1"
    assert user.jwt_token == "Bearer user-token"
    assert session_manager.effective_token_calls == [("user-1", "Bearer user-token")]


@pytest.mark.asyncio
async def test_current_user_cookie_remains_preferred_over_bearer(monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: False)
    session_manager = FakeSessionManager()
    request = _request(
        cookies={"auth_token": "Bearer browser-token"},
        headers={"Authorization": "Bearer forwarded-token"},
    )

    await get_current_user(request, session_manager=session_manager)

    assert session_manager.effective_token_calls == [("user-1", "Bearer browser-token")]


@pytest.mark.asyncio
async def test_current_user_rejects_missing_or_invalid_bearer(monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: False)

    class InvalidSessionManager(FakeSessionManager):
        def get_user_from_token(self, token):
            return None

    from fastapi import HTTPException

    for request in (_request(), _request(headers={"Authorization": "Bearer expired-token"})):
        with pytest.raises(HTTPException, match="Authentication required") as exc:
            await get_current_user(request, session_manager=InvalidSessionManager())
        assert exc.value.status_code == 401
