"""Temporary passwords cannot confer workspace, API, or OpenSearch access."""

import asyncio
import secrets

import pytest
from fastapi import HTTPException

from db.models import LocalCredential
from services.local_auth_service import create_local_user, issue_session
from tests.unit import test_local_auth as shared_auth

local_stack = shared_auth.local_stack


@pytest.mark.asyncio
async def test_temporary_admin_must_replace_password_before_any_access(local_stack):
    stack = local_stack
    initial = (
        "initialpass"  # Operator-only temporary passwords may be shorter than final passwords.
    )
    async with stack.factory() as session:
        row = await create_local_user(
            session, login="new-admin", password=initial, role="admin", require_password_change=True
        )
        await session.commit()
        uid = row.id
        with pytest.raises(HTTPException, match="Password replacement required"):
            await issue_session(session, stack.manager, row)
    client = stack.client
    result = await client.post(
        "/auth/local/login", json={"login": "new-admin", "password": initial}
    )
    assert result.status_code == 200 and result.json()["password_change_required"]
    assert not result.json()["authenticated"]
    assert "auth_token" not in client.cookies
    proof = client.cookies.get("password_change_token")
    assert proof and stack.manager.verify_token(proof) is None
    assert (await client.get("/auth/me")).json()["password_change_required"]
    assert (await client.get("/users/me")).status_code == 401
    assert (await client.get("/users/local")).status_code == 401
    assert (
        await client.get("/v1/principal", headers={"Authorization": f"Bearer {proof}"})
    ).status_code == 401
    assert (
        await client.post("/auth/local/password/required", json={"password": initial})
    ).status_code == 400
    assert (
        await client.post("/auth/local/password/required", json={"password": "short"})
    ).status_code == 400
    assert (
        await client.post(
            "/auth/local/password/required",
            json={"password": secrets.token_urlsafe(24)},
            headers={"Origin": "https://evil.example"},
        )
    ).status_code == 403
    replacement = secrets.token_urlsafe(24)
    result = await client.post("/auth/local/password/required", json={"password": replacement})
    assert result.status_code == 200 and result.json()["authenticated"]
    assert not client.cookies.get("password_change_token")
    me = (await client.get("/users/me")).json()
    assert me["user_id"] == uid and me["roles"] == ["admin"]
    async with stack.factory() as session:
        assert not (await session.get(LocalCredential, uid)).must_change_password
    client.cookies.clear()
    assert (
        await client.post("/auth/local/login", json={"login": "new-admin", "password": initial})
    ).status_code == 401
    client.cookies.set("password_change_token", proof)
    assert (
        await client.post(
            "/auth/local/password/required", json={"password": secrets.token_urlsafe(24)}
        )
    ).status_code == 401
    client.cookies.clear()
    assert (
        await client.post("/auth/local/login", json={"login": "new-admin", "password": replacement})
    ).status_code == 200


@pytest.mark.asyncio
async def test_only_one_concurrent_password_replacement_can_win(local_stack):
    stack = local_stack
    async with stack.factory() as session:
        await create_local_user(
            session, login="temporary", password="temporary-pass", require_password_change=True
        )
        await session.commit()
    await stack.client.post(
        "/auth/local/login", json={"login": "temporary", "password": "temporary-pass"}
    )
    responses = await asyncio.gather(
        *(
            stack.client.post(
                "/auth/local/password/required", json={"password": secrets.token_urlsafe(24)}
            )
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 401]


@pytest.mark.asyncio
async def test_normal_login_clears_another_accounts_pending_proof(local_stack):
    stack = local_stack
    normal_password = secrets.token_urlsafe(24)
    async with stack.factory() as session:
        await create_local_user(
            session, login="pending", password="pending-password", require_password_change=True
        )
        await create_local_user(session, login="normal", password=normal_password)
        await session.commit()
    await stack.client.post(
        "/auth/local/login", json={"login": "pending", "password": "pending-password"}
    )
    assert stack.client.cookies.get("password_change_token")
    response = await stack.client.post(
        "/auth/local/login", json={"login": "normal", "password": normal_password}
    )
    assert response.status_code == 200 and response.json()["authenticated"]
    assert not stack.client.cookies.get("password_change_token")
    assert not (await stack.client.get("/auth/me")).json()["password_change_required"]
