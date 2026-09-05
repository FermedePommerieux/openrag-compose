"""Real local account and cookie requests; no fabricated login JWTs."""

import secrets
import time
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from api import auth, local_auth, users
from auth.local_admin import bootstrap_admin
from config import settings
from config.auth_mode import (
    google_login_enabled,
    local_auth_enabled,
    validate_auth_configuration,
)
from db import engine
from db.models import AuthSession, LocalCredential
from db.models import User as UserRow
from db.seed import seed_roles_and_permissions
from dependencies import get_api_key_user_async
from services.auth_service import AuthService
from services.local_auth_service import (
    _ATTEMPTS,
    hash_password,
    throttle_login,
    verify_password,
)
from services.rbac_service import RBACService
from services.user_service import ensure_user_row
from session_manager import SessionManager, User


@pytest_asyncio.fixture
async def local_stack(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", "local")
    monkeypatch.setenv("OPENRAG_RBAC_ENFORCE", "true")
    monkeypatch.setenv("OPENRAG_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("JWT_SIGNING_KEY", secrets.token_urlsafe(48))
    monkeypatch.setattr(settings, "IBM_AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
    _ATTEMPTS.clear()
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/openrag.db")
    async with db.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr(engine, "_engine", db)
    monkeypatch.setattr(engine, "SessionLocal", factory)
    manager = SessionManager()
    app = FastAPI()
    app.state.services = {
        "session_manager": manager,
        "auth_service": AuthService(manager),
        "rbac_service": RBACService(factory),
        "api_key_service": SimpleNamespace(validate_key=AsyncMock()),
    }
    app.include_router(local_auth.router)
    app.include_router(users.router)
    app.add_api_route("/auth/me", auth.auth_me, methods=["GET"])
    app.add_api_route("/auth/logout", auth.auth_logout, methods=["POST"])

    @app.get("/v1/principal")
    async def sdk_principal(user=Depends(get_api_key_user_async)):
        return {"user_id": user.user_id, "db_user_id": user.db_user_id}

    password = secrets.token_urlsafe(24)
    async with factory() as session:
        await seed_roles_and_permissions(session)
        admin = await bootstrap_admin(session, "operator", password)
        await session.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    )
    login = await client.post("/auth/local/login", json={"login": "operator", "password": password})
    assert login.status_code == 200
    yield SimpleNamespace(
        client=client,
        factory=factory,
        manager=manager,
        app=app,
        admin_id=admin.id,
        admin_password=password,
    )
    await client.aclose()
    await db.dispose()


async def create_account(stack, login="reader-a"):
    password = secrets.token_urlsafe(24)
    response = await stack.client.post("/users/local", json={"login": login, "password": password})
    assert response.status_code == 201, response.text
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url="https://testserver"
    )
    response = await client.post("/auth/local/login", json={"login": login, "password": password})
    assert response.status_code == 200
    return client, response.json()["user_id"], password


@pytest.mark.asyncio
async def test_real_two_local_users_and_restart(local_stack):
    a, aid, _ = await create_account(local_stack)
    b, bid, _ = await create_account(local_stack, "reader-b")
    async with aclosing(a), aclosing(b):
        assert len({aid, bid, local_stack.admin_id}) == 3
        local_stack.manager.users.clear()  # Simulate loss of the process user registry.
        for client, uid in [(a, aid), (b, bid)]:
            response = await client.get("/users/me")
            assert response.status_code == 200
            data = response.json()
            assert data["user_id"] == uid and data["authenticated"]
            assert data["roles"] == ["user"] and data["workspace"] == "default"
            assert data["provider"] == "local"
            assert "hash" not in response.text and "password" not in response.text
            token = client.cookies.get("auth_token")
            claims = local_stack.manager.verify_token(token)
            assert claims["sub"] == uid and claims["roles"] == ["openrag_user"]
            assert "all_access" not in claims["user_roles"]
            result = await client.get("/v1/principal", headers={"Authorization": f"Bearer {token}"})
            assert result.json() == {"user_id": uid, "db_user_id": uid}


@pytest.mark.asyncio
async def test_password_hash_and_constant_library_verification():
    password = secrets.token_urlsafe(24)
    first, second = await hash_password(password), await hash_password(password)
    assert first.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert first != second and password not in first
    assert await verify_password(password, first)
    assert not await verify_password("incorrect", first)
    assert not await verify_password("incorrect", None)
    assert not await verify_password("incorrect", "malformed")
    for weak in ["short", "x" * 1025]:
        with pytest.raises(ValueError):
            await hash_password(weak)


@pytest.mark.asyncio
async def test_wrong_unknown_disabled_and_session_revocation(local_stack):
    client, uid, password = await create_account(local_stack)
    async with aclosing(client):
        original_token = client.cookies.get("auth_token")
        errors = []
        for login, pwd in [("reader-a", "incorrect"), ("missing-user", "incorrect")]:
            result = await client.post("/auth/local/login", json={"login": login, "password": pwd})
            errors.append((result.status_code, result.json()))
        disabled = await local_stack.client.patch(f"/users/local/{uid}", json={"enabled": False})
        assert disabled.status_code == 200
        result = await client.post(
            "/auth/local/login", json={"login": "reader-a", "password": password}
        )
        errors.append((result.status_code, result.json()))
        assert errors == [(401, {"detail": "Invalid login or password"})] * 3
        assert (await client.get("/users/me")).status_code == 401
        await local_stack.client.patch(f"/users/local/{uid}", json={"enabled": True})
        assert (await client.get("/users/me")).status_code == 401
        client.cookies.clear()
        assert (
            await client.get("/users/me", headers={"Authorization": f"Bearer {original_token}"})
        ).status_code == 401


@pytest.mark.asyncio
async def test_change_reset_and_logout_revoke_backend_sessions(local_stack):
    client, uid, password = await create_account(local_stack)
    async with aclosing(client):
        changed = secrets.token_urlsafe(24)
        token = client.cookies.get("auth_token")
        response = await client.post(
            "/auth/local/password", json={"current_password": password, "password": changed}
        )
        assert response.status_code == 200
        assert (
            await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 401
        assert (
            await client.post("/auth/local/login", json={"login": "reader-a", "password": changed})
        ).status_code == 200
        reset = secrets.token_urlsafe(24)
        result = await local_stack.client.post(
            f"/users/local/{uid}/password", json={"password": reset}
        )
        assert result.status_code == 200
        assert (await client.get("/users/me")).status_code == 401
        assert (
            await client.post("/auth/local/login", json={"login": "reader-a", "password": reset})
        ).status_code == 200
        token = client.cookies.get("auth_token")
        assert (await client.post("/auth/logout")).status_code == 200
        assert (
            await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 401


@pytest.mark.asyncio
async def test_admin_boundaries_and_bootstrap_once(local_stack, monkeypatch):
    client, uid, _ = await create_account(local_stack)
    async with aclosing(client):
        for bypass in ["true", "false"]:
            monkeypatch.setenv("OPENRAG_RBAC_ENFORCE", bypass)
            assert (await client.get("/users/local")).status_code == 403
            assert (
                await client.post(
                    f"/users/local/{uid}/password", json={"password": secrets.token_urlsafe(24)}
                )
            ).status_code == 403
        listing = await local_stack.client.get("/users/local")
        assert listing.status_code == 200 and len(listing.json()["users"]) == 2
        assert "password" not in listing.text and "hash" not in listing.text
        async with local_stack.factory() as session:
            with pytest.raises(ValueError, match="already completed"):
                await bootstrap_admin(session, "another-admin", secrets.token_urlsafe(24))
        result = await local_stack.client.post(
            "/users/local", json={"login": "READER-A", "password": secrets.token_urlsafe(24)}
        )
        assert result.status_code == 409


@pytest.mark.asyncio
async def test_session_expiry_malformed_anonymous_and_origin(local_stack):
    client, uid, _ = await create_account(local_stack)
    async with aclosing(client):
        token = client.cookies.get("auth_token")
        sid = local_stack.manager.verify_token(token)["sid"]
        async with local_stack.factory() as session:
            record = await session.get(AuthSession, sid)
            record.expires_at = int(time.time()) - 1
            session.add(record)
            await session.commit()
        assert (await client.get("/users/me")).status_code == 401
        client.cookies.clear()
        for malformed in ["", "not.a.jwt"]:
            assert (
                await client.get("/users/me", headers={"Authorization": f"Bearer {malformed}"})
            ).status_code == 401
        assert (
            await client.post(
                "/auth/local/login",
                json={"login": "operator", "password": local_stack.admin_password},
                headers={"Origin": "https://evil.invalid"},
            )
        ).status_code == 403
        assert (
            await local_stack.client.post("/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
        ).status_code == 403


@pytest.mark.asyncio
async def test_local_only_has_no_external_network_and_secure_cookie(local_stack, monkeypatch):
    monkeypatch.setattr(
        "session_manager.httpx.AsyncClient.get",
        AsyncMock(side_effect=AssertionError("external network")),
    )
    # Use ASGI transport directly; class patch above also patches our GET method.
    response = await local_stack.client.request("GET", "/auth/me")
    assert response.json()["local_auth_enabled"] and not response.json()["google_auth_enabled"]
    response = await local_stack.client.post(
        "/auth/local/login", json={"login": "operator", "password": local_stack.admin_password}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ["secure", "httponly", "samesite=lax"])
    validate_auth_configuration()


@pytest.mark.asyncio
async def test_optional_provider_outage_keeps_local_login_and_session(local_stack, monkeypatch):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", "local_plus_external")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "configured")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "configured")
    monkeypatch.setattr(
        local_stack.manager,
        "get_user_info_from_token",
        AsyncMock(side_effect=httpx.ConnectError("Provider unavailable")),
    )
    with pytest.raises(httpx.ConnectError):
        await local_stack.manager.create_user_session("unavailable", "https://testserver")
    response = await local_stack.client.post(
        "/auth/local/login", json={"login": "operator", "password": local_stack.admin_password}
    )
    assert response.status_code == 200
    assert (await local_stack.client.get("/users/me")).json()["user_id"] == local_stack.admin_id


@pytest.mark.asyncio
async def test_local_only_service_container_starts_without_external_auth(
    local_stack, monkeypatch, tmp_path
):
    from app import container

    monkeypatch.setenv("OPENRAG_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("OPENRAG_CONFIG_PATH", str(tmp_path / "config"))
    monkeypatch.setattr(container, "JWT_SIGNING_KEY", "configured-for-test")
    monkeypatch.setattr(container.clients, "initialize", AsyncMock())
    monkeypatch.setattr(container.TelemetryClient, "send_event", AsyncMock())
    monkeypatch.setattr(container.ConnectorService, "initialize", AsyncMock())
    monkeypatch.setattr("services.workspace_config_service.WorkspaceConfigService", MagicMock())
    # Auth-provider discovery/userinfo must not be part of constructing services.
    network = AsyncMock(side_effect=AssertionError("External auth network forbidden"))
    monkeypatch.setattr(SessionManager, "get_user_info_from_token", network)
    services = await container.initialize_services()
    assert isinstance(services["auth_service"], AuthService)
    assert isinstance(services["session_manager"], SessionManager)
    network.assert_not_called()
    assert not settings.is_no_auth_mode()


@pytest.mark.asyncio
async def test_api_key_delegation_keeps_principal_and_checks_account(local_stack):
    client, uid, _ = await create_account(local_stack)
    async with aclosing(client):
        local_stack.app.state.services["api_key_service"].validate_key.return_value = {
            "user_id": uid,
            "user_email": "",
            "key_id": "controlled-key",
        }
        response = await client.get(
            "/v1/principal", headers={"Authorization": "Bearer orag_controlled"}
        )
        assert response.json() == {"user_id": uid, "db_user_id": uid}
        delegated = local_stack.manager.users[uid]
        assert delegated.session_id and delegated.provider == "local"
        token = delegated.jwt_token
        claims = local_stack.manager.verify_token(token)
        assert 0 < claims["exp"] - time.time() <= 300
        callback = await client.get("/v1/principal", headers={"Authorization": token})
        assert callback.json()["user_id"] == uid
        await local_stack.client.patch(f"/users/local/{uid}", json={"enabled": False})
        for authorization in [token, "Bearer orag_controlled"]:
            response = await client.get("/v1/principal", headers={"Authorization": authorization})
            assert response.status_code == 401


@pytest.mark.parametrize(
    "mode,google,noauth,local",
    [
        ("local", False, False, True),
        ("local", True, False, True),
        ("local_plus_external", False, False, True),
        ("local_plus_external", True, False, True),
        ("external", True, False, False),
        ("auto", False, True, False),
        ("auto", True, False, False),
        ("no_auth", True, True, False),
    ],
)
def test_auth_modes(monkeypatch, mode, google, noauth, local):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", mode)
    monkeypatch.setattr(settings, "IBM_AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "configured" if google else "")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "configured" if google else "")
    assert settings.is_no_auth_mode() is noauth
    assert local_auth_enabled() is local
    assert google_login_enabled() == (
        google and mode in {"auto", "external", "local_plus_external"}
    )
    validate_auth_configuration()


@pytest.mark.parametrize(
    "mode,rbac,ibm",
    [
        ("external", "true", False),
        ("local", "false", False),
        ("local", "true", True),
        ("bad", "true", False),
    ],
)
def test_bad_auth_configuration_fails_closed(monkeypatch, mode, rbac, ibm):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", mode)
    monkeypatch.setenv("OPENRAG_RBAC_ENFORCE", rbac)
    monkeypatch.setattr(settings, "IBM_AUTH_ENABLED", ibm)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
    with pytest.raises((ValueError, RuntimeError)):
        validate_auth_configuration()


def test_login_throttle():
    _ATTEMPTS.clear()
    for _ in range(10):
        throttle_login("bounded-user", "test-client")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        throttle_login("bounded-user", "other-client")
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_google_mapping_preserves_ids_and_does_not_link_local(local_stack, monkeypatch):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", "local_plus_external")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "configured")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "configured")
    async with local_stack.factory() as session:
        # A subject collision must resolve to the persisted distinct internal ID.
        external = await ensure_user_row(
            session,
            User(
                user_id=local_stack.admin_id,
                email="operator@example.invalid",
                name="External",
                provider="google",
            ),
        )
        await session.commit()
        assert external.id != local_stack.admin_id
    monkeypatch.setattr(
        local_stack.manager,
        "get_user_info_from_token",
        AsyncMock(
            return_value={
                "id": local_stack.admin_id,
                "email": "operator@example.invalid",
                "name": "External",
            }
        ),
    )
    token = await local_stack.manager.create_user_session(
        "verified-provider-access-token", "https://testserver"
    )
    local_stack.manager.users.clear()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=local_stack.app), base_url="https://testserver"
    )
    async with aclosing(client):
        result = await client.get("/users/me", headers={"Authorization": token})
        assert result.status_code == 200 and result.json()["user_id"] == external.id
        async with local_stack.factory() as session:
            assert await session.get(LocalCredential, external.id) is None
            assert (await session.get(UserRow, local_stack.admin_id)).oauth_provider == "local"
