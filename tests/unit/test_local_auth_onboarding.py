"""First-run account choice uses real product routes and durable authority."""

import asyncio
import secrets
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from api import auth, local_auth, users
from config import auth_mode, settings
from config.config_manager import OpenRAGConfig
from db import engine
from db.models import LocalCredential, MigrationStatus, User
from db.repositories.workspace_config_repo import WorkspaceConfigRepo
from db.seed import seed_roles_and_permissions
from services.auth_service import AuthService
from services.local_auth_onboarding import SETUP_MARKER, load_onboarding_auth_policy
from services.rbac_service import RBACService, is_rbac_enforced
from session_manager import SessionManager


@pytest_asyncio.fixture
async def first_run(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", "auto")
    monkeypatch.setenv("OPENRAG_RBAC_ENFORCE", "false")
    monkeypatch.setenv("OPENRAG_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("JWT_SIGNING_KEY", secrets.token_urlsafe(48))
    monkeypatch.setattr(settings, "IBM_AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
    monkeypatch.setattr(settings.config_manager, "_config", OpenRAGConfig.from_dict({}))
    monkeypatch.setattr(auth_mode, "_onboarding_local_enabled", False)
    monkeypatch.setattr(local_auth, "_SETUP_LOCK", asyncio.Lock())
    from services.local_auth_service import _ATTEMPTS

    _ATTEMPTS.clear()
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/users.db")
    async with db.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr(engine, "_engine", db)
    monkeypatch.setattr(engine, "SessionLocal", factory)
    async with factory() as session:
        await seed_roles_and_permissions(session)
        await session.commit()
    manager = SessionManager()
    app = FastAPI()
    app.state.services = {
        "session_manager": manager,
        "auth_service": AuthService(manager),
        "rbac_service": RBACService(factory),
    }
    app.include_router(local_auth.router)
    app.include_router(users.router)
    app.add_api_route("/auth/me", auth.auth_me, methods=["GET"])
    app.add_api_route("/auth/logout", auth.auth_logout, methods=["POST"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield SimpleNamespace(client=client, factory=factory, app=app, manager=manager)
    await db.dispose()


def credentials(login="first-admin"):
    return {"login": login, "password": secrets.token_urlsafe(24)}


@pytest.mark.asyncio
async def test_create_first_admin_activates_local_rbac_and_survives_restart(first_run):
    client = first_run.client
    status = (await client.get("/auth/me")).json()
    assert (
        status["no_auth_mode"]
        and status["local_setup_available"]
        and status["local_setup_can_skip"]
    )
    assert not is_rbac_enforced()
    body = credentials()
    response = await client.post("/auth/local/setup", json={**body, "role": "viewer"})
    assert response.status_code == 201
    assert "secure" in response.headers["set-cookie"].lower()
    me = (await client.get("/users/me")).json()
    assert me["authenticated"] and me["roles"] == ["admin"] and me["rbac_enforced"]
    assert me["user_id"] == response.json()["user_id"]
    assert auth_mode.local_auth_enabled() and not settings.is_no_auth_mode()
    assert not (await client.get("/auth/me")).json()["local_setup_available"]
    auth_mode.set_onboarding_local_auth(False)
    await load_onboarding_auth_policy()
    assert is_rbac_enforced() and not settings.is_no_auth_mode()
    assert (await client.get("/users/me")).json()["user_id"] == me["user_id"]
    client.cookies.clear()
    assert (await client.get("/users/me")).status_code == 401
    assert (await client.post("/auth/local/setup", json=credentials("intruder"))).status_code == 409
    assert (await client.post("/auth/local/login", json=body)).status_code == 200


@pytest.mark.asyncio
async def test_optional_skip_is_durable_and_does_not_create_accounts(first_run):
    response = await first_run.client.post("/auth/local/setup/skip")
    assert response.status_code == 200
    await load_onboarding_auth_policy()
    assert settings.is_no_auth_mode() and not is_rbac_enforced()
    assert not (await first_run.client.get("/auth/me")).json()["local_setup_available"]
    assert (await first_run.client.post("/auth/local/setup", json=credentials())).status_code == 409
    async with first_run.factory() as session:
        assert (await session.get(MigrationStatus, SETUP_MARKER)).notes == "skipped"
        assert not (await session.execute(select(LocalCredential))).first()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["existing_user", "other_none_user", "edited", "started", "bootstrap"]
)
async def test_existing_workspace_cannot_be_claimed(first_run, reason):
    async with first_run.factory() as session:
        if reason == "existing_user":
            session.add(
                User(id="external-id", oauth_provider="google", oauth_subject="external-id")
            )
        elif reason == "other_none_user":
            session.add(User(id="real-id", oauth_provider="none", oauth_subject="real-id"))
        elif reason == "bootstrap":
            session.add(MigrationStatus(name="local_admin_bootstrap_v1"))
        else:
            await WorkspaceConfigRepo(session).upsert(
                "meta" if reason == "edited" else "onboarding",
                {"edited": True} if reason == "edited" else {"current_step": 1},
            )
        await session.commit()
    assert not (await first_run.client.get("/auth/me")).json()["local_setup_available"]
    assert (await first_run.client.post("/auth/local/setup", json=credentials())).status_code == 409


@pytest.mark.asyncio
async def test_upgrade_closes_setup_even_after_provider_wizard_reset(first_run):
    async with first_run.factory() as session:
        await WorkspaceConfigRepo(session).upsert("meta", {"edited": True})
        await session.commit()
    await load_onboarding_auth_policy()
    assert settings.is_no_auth_mode() and not is_rbac_enforced()
    async with first_run.factory() as session:
        assert (await session.get(MigrationStatus, SETUP_MARKER)).notes == "closed"
        await WorkspaceConfigRepo(session).upsert("meta", {"edited": False})
        await WorkspaceConfigRepo(session).upsert("onboarding", {"current_step": 0})
        await session.commit()
    await load_onboarding_auth_policy()
    assert not (await first_run.client.get("/auth/me")).json()["local_setup_available"]
    assert (await first_run.client.post("/auth/local/setup", json=credentials())).status_code == 409


@pytest.mark.asyncio
async def test_fresh_anonymous_principal_does_not_close_setup(first_run):
    async with first_run.factory() as session:
        session.add(User(id="anonymous", oauth_provider="none", oauth_subject="anonymous"))
        await session.commit()
    await load_onboarding_auth_policy()
    assert (await first_run.client.get("/auth/me")).json()["local_setup_available"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["external", "no_auth"])
async def test_explicit_deployment_mode_is_not_overridden(first_run, monkeypatch, mode):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", mode)
    assert not (await first_run.client.get("/auth/me")).json()["local_setup_available"]
    assert (await first_run.client.post("/auth/local/setup", json=credentials())).status_code == 409
    auth_mode.set_onboarding_local_auth(True)
    assert auth_mode.get_auth_mode() == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["local", "local_plus_external"])
async def test_explicit_local_mode_allows_first_admin_but_not_anonymous_skip(
    first_run, monkeypatch, mode
):
    monkeypatch.setenv("OPENRAG_AUTH_MODE", mode)
    monkeypatch.setenv("OPENRAG_RBAC_ENFORCE", "true")
    status = (await first_run.client.get("/auth/me")).json()
    assert status["local_setup_available"] and not status["local_setup_can_skip"]
    assert (await first_run.client.post("/auth/local/setup/skip")).status_code == 409
    assert (await first_run.client.post("/auth/local/setup", json=credentials())).status_code == 201
    assert (await first_run.client.get("/users/me")).json()["roles"] == ["admin"]


@pytest.mark.asyncio
async def test_invalid_password_origin_and_concurrent_claims(first_run):
    client = first_run.client
    assert (
        await client.post("/auth/local/setup", json={"login": "admin", "password": "short"})
    ).status_code == 400
    assert (await client.get("/auth/me")).json()["local_setup_available"]
    assert (
        await client.post(
            "/auth/local/setup", json=credentials(), headers={"Origin": "https://evil.invalid"}
        )
    ).status_code == 403
    results = await asyncio.gather(
        *[client.post("/auth/local/setup", json=credentials(f"admin-{i}")) for i in range(2)]
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    async with first_run.factory() as session:
        assert len((await session.execute(select(LocalCredential))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_invalid_persisted_choice_fails_startup(first_run):
    async with first_run.factory() as session:
        session.add(MigrationStatus(name=SETUP_MARKER, notes="invalid"))
        await session.commit()
    with pytest.raises(RuntimeError, match="Invalid persisted"):
        await load_onboarding_auth_policy()
