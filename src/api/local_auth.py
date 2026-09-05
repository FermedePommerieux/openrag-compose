"""Local login and minimal administrative operations; no public registration."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from config.auth_mode import local_auth_enabled, secure_auth_cookie
from db.models import LocalCredential
from db.models import User as UserRow
from db.repositories import AuditRepo, RoleRepo
from dependencies import get_current_user, get_db_session, get_session_manager
from services.local_auth_service import (
    SESSION_SECONDS,
    create_local_user,
    login_local,
    reset_password,
    revoke_sessions,
    throttle_login,
    verify_password,
)
from session_manager import User

router = APIRouter(tags=["local authentication"])


def require_local_mode() -> None:
    if not local_auth_enabled():
        raise HTTPException(404, "Local authentication is not enabled")


def check_browser_origin(request: Request) -> None:
    """Reject cross-site browser credential mutations; CLI clients need no Origin."""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site authentication request")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-site authentication request")


class LoginBody(BaseModel):
    login: str
    password: SecretStr


class CreateBody(LoginBody):
    role: str = "user"


class PasswordBody(BaseModel):
    password: SecretStr


class ChangePasswordBody(PasswordBody):
    current_password: SecretStr


class ActiveBody(BaseModel):
    enabled: bool


async def account_view(session: AsyncSession, row: UserRow) -> dict:
    credential = await session.get(LocalCredential, row.id)
    return {
        "user_id": row.id,
        "login": credential.login if credential else None,
        "enabled": row.is_active,
        "provider": row.oauth_provider,
        "roles": [r.name for r in await RoleRepo(session).list_user_roles(row.id)],
        "workspace": "default",
    }


async def require_user_admin(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    require_local_mode()
    check_browser_origin(request)
    # Account administration ALWAYS checks live workspace permissions, including
    # deployments with the legacy RBAC bypass; never trust role claims from a body.
    perms = await RoleRepo(session).list_permissions_for_user(user.db_user_id or user.user_id)
    if not {"users:invite", "roles:assign"}.issubset(perms):
        raise HTTPException(403, "User administration permission required")
    return user


@router.post("/auth/local/login")
async def local_login(
    body: LoginBody,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    manager=Depends(get_session_manager),
):
    require_local_mode()
    check_browser_origin(request)
    throttle_login(
        body.login.strip().lower()[:64], request.client.host if request.client else "unknown"
    )
    token, principal = await login_local(
        session, manager, body.login, body.password.get_secret_value()
    )
    await session.commit()
    response = JSONResponse({"authenticated": True, "user_id": principal.user_id})
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        "auth_token",
        token.removeprefix("Bearer "),
        httponly=True,
        secure=secure_auth_cookie(),
        samesite="lax",
        max_age=SESSION_SECONDS,
    )
    return response


@router.post("/auth/local/password")
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    require_local_mode()
    check_browser_origin(request)
    throttle_login(user.user_id, request.client.host if request.client else "unknown")
    credential = await session.get(LocalCredential, user.user_id)
    if credential is None or not await verify_password(
        body.current_password.get_secret_value(),
        credential.password_hash,
    ):
        raise HTTPException(401, "Invalid login or password")
    try:
        await reset_password(session, user.user_id, body.password.get_secret_value(), user.user_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    await session.commit()
    response = JSONResponse({"status": "password_changed", "reauthentication_required": True})
    response.delete_cookie("auth_token")
    return response


@router.get("/users/local")
async def list_local_users(
    user: User = Depends(require_user_admin),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    rows = (
        (
            await session.execute(
                select(UserRow)
                .join(LocalCredential, col(LocalCredential.user_id) == col(UserRow.id))
                .order_by(col(UserRow.id))
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"users": [await account_view(session, row) for row in rows]}


@router.post("/users/local", status_code=201)
async def create_user(
    body: CreateBody,
    user: User = Depends(require_user_admin),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        row = await create_local_user(
            session,
            login=body.login,
            password=body.password.get_secret_value(),
            role=body.role,
            actor_id=user.user_id,
        )
        result = await account_view(session, row)
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Local login already exists") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post("/users/local/{user_id}/password")
async def admin_reset_password(
    user_id: str,
    body: PasswordBody,
    user: User = Depends(require_user_admin),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        await reset_password(session, user_id, body.password.get_secret_value(), user.user_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    await session.commit()
    return {"status": "password_reset", "sessions_revoked": True}


@router.patch("/users/local/{user_id}")
async def set_user_active(
    user_id: str,
    body: ActiveBody,
    user: User = Depends(require_user_admin),
    session: AsyncSession = Depends(get_db_session),
):
    credential = await session.get(LocalCredential, user_id)
    row = await session.get(UserRow, user_id)
    if credential is None or row is None:
        raise HTTPException(404, "Local account not found")
    if user_id == user.user_id and not body.enabled:
        raise HTTPException(400, "Cannot disable your own administrator account")
    row.is_active = body.enabled
    session.add(row)
    # Increment even on re-enable: pre-disable logins racing this transaction
    # cannot resurrect a session after the account is enabled again.
    await session.execute(
        update(LocalCredential)
        .where(col(LocalCredential.user_id) == user_id)
        .values(version=col(LocalCredential.version) + 1)
    )
    await revoke_sessions(session, user_id)
    await AuditRepo(session).write(
        event="user.status_changed",
        actor_user_id=user.user_id,
        target_type="user",
        target_id=user_id,
        audit_metadata={"enabled": body.enabled},
    )
    await session.commit()
    return await account_view(session, row)
