"""Local credentials reuse OpenRAG users, roles, SQL persistence and JWT signing."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cachetools import TTLCache
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from db.models import AuthSession, LocalCredential, User
from db.repositories import AuditRepo, RoleRepo, UserRepo
from session_manager import User as Principal

# RFC 9106 low-memory profile: Argon2id, 64 MiB, t=3, p=4. Standard library
# generates salts and verifies in constant time. Bound concurrent memory use.
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
_HASH_SLOTS = asyncio.Semaphore(2)
SESSION_SECONDS = 8 * 60 * 60
_ATTEMPTS: TTLCache[str, int] = TTLCache(maxsize=8192, ttl=300)


def normalize_login(login: str) -> str:
    login = login.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", login):
        raise ValueError("Login must be 3–64 ASCII letters, digits, dots, hyphens or underscores")
    return login


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")


async def hash_password(password: str, *, temporary: bool = False) -> str:
    if temporary:
        if not 8 <= len(password) <= 1024:
            raise ValueError("Temporary password must contain 8–1024 characters")
    else:
        validate_password(password)
    async with _HASH_SLOTS:
        return await asyncio.to_thread(PASSWORD_HASHER.hash, password)


async def verify_password(password: str, encoded: str | None) -> bool:
    if len(password) > 1024:
        return False
    async with _HASH_SLOTS:
        try:
            valid = await asyncio.to_thread(
                PASSWORD_HASHER.verify, encoded or _DUMMY_HASH, password
            )
            return bool(encoded and valid)
        except (VerificationError, InvalidHashError):
            return False


def throttle_login(login: str, remote: str) -> None:
    # Do not trust caller-controlled X-Forwarded-For. A trusted reverse proxy
    # should also enforce a per-client limit. The global bucket bounds cache churn.
    buckets = [("global", 500), (f"login:{login}", 10), (f"remote:{remote}", 100)]
    if any(_ATTEMPTS.get(key, 0) >= limit for key, limit in buckets):
        raise HTTPException(429, "Too many authentication attempts", headers={"Retry-After": "300"})
    for key, _ in buckets:
        _ATTEMPTS[key] = _ATTEMPTS.get(key, 0) + 1


def principal_from_row(row: User) -> Principal:
    return Principal(
        user_id=row.id,
        db_user_id=row.id,
        email=row.email or "",
        name=row.display_name or "",
        picture=row.picture_url,
        provider=row.oauth_provider,
        auth_subject=row.oauth_subject,
    )


async def create_local_user(
    session: AsyncSession,
    *,
    login: str,
    password: str,
    role: str = "user",
    actor_id: str | None = None,
    require_password_change: bool = False,
    planned_user_id: str | None = None,
) -> User:
    login = normalize_login(login)
    role_row = await RoleRepo(session).get_by_name(role)
    if role_row is None:
        raise ValueError("Unknown workspace role")
    encoded = await hash_password(password, temporary=require_password_change)
    if planned_user_id is not None and uuid.UUID(planned_user_id).version != 4:
        raise ValueError("Planned user ID must be a random UUID v4")
    user_id = str(uuid.UUID(planned_user_id)) if planned_user_id else str(uuid.uuid4())
    # oauth_subject remains immutable. The editable/login identifier lives only
    # on the method row; unverified local email aliases cannot grant document ACLs.
    row = User(id=user_id, oauth_provider="local", oauth_subject=user_id, display_name=login)
    await UserRepo(session).add(row)
    session.add(
        LocalCredential(
            user_id=row.id,
            login=login,
            password_hash=encoded,
            must_change_password=require_password_change,
        )
    )
    await session.flush()
    await RoleRepo(session).assign_role(row.id, role_row.id, granted_by=actor_id)
    await AuditRepo(session).write(
        event="user.local_created",
        actor_user_id=actor_id or row.id,
        target_type="user",
        target_id=row.id,
        audit_metadata={"role": role},
    )
    return row


async def issue_session(
    session: AsyncSession, manager, row: User, *, ttl_seconds: int = SESSION_SECONDS
) -> tuple[str, Principal]:
    if not row.is_active:
        raise HTTPException(401, "Authentication required")
    credential = await session.get(LocalCredential, row.id)
    if credential is not None and credential.must_change_password:
        raise HTTPException(403, "Password replacement required")
    sid = str(uuid.uuid4())
    principal = principal_from_row(row)
    principal.session_id = sid
    record = AuthSession(
        id=sid,
        user_id=row.id,
        expires_at=int(time.time()) + ttl_seconds,
        credential_version=credential.version if credential else None,
    )
    session.add(record)
    await session.execute(
        delete(AuthSession).where(col(AuthSession.expires_at) <= int(time.time()))
    )
    row.last_login = datetime.now(UTC)
    session.add(row)
    token = manager._create_signed_jwt_token(
        principal,
        expires_delta=timedelta(seconds=ttl_seconds),
    )
    if not token:
        raise RuntimeError("Session signing is unavailable")
    await session.flush()
    # The durable session is authoritative; the registry is only a compatibility cache.
    manager.users[row.id] = principal
    return token, principal


async def login_local(session: AsyncSession, manager, login: str, password: str):
    try:
        canonical_login = normalize_login(login)
    except ValueError:
        canonical_login = ""
    result = await session.execute(
        select(LocalCredential).where(col(LocalCredential.login) == canonical_login)
    )
    credential = result.scalar_one_or_none()
    valid = await verify_password(password, credential.password_hash if credential else None)
    row = await session.get(User, credential.user_id) if credential else None
    if not valid or row is None or not row.is_active:
        raise HTTPException(401, "Invalid login or password")
    if PASSWORD_HASHER.check_needs_rehash(credential.password_hash):
        # Do not overwrite an administrator reset racing with verification.
        encoded = await hash_password(password, temporary=credential.must_change_password)
        await session.execute(
            update(LocalCredential)
            .where(
                col(LocalCredential.user_id) == row.id,
                col(LocalCredential.version) == credential.version,
            )
            .values(password_hash=encoded)
        )
    if credential.must_change_password:
        # This opaque proof cannot authenticate to OpenSearch, APIs or tools.
        # Only its hash is persisted, and only the replacement route accepts it.
        proof = secrets.token_urlsafe(32)
        principal = principal_from_row(row)
        principal.must_change_password = True
        session.add(
            AuthSession(
                id=hashlib.sha256(proof.encode()).hexdigest(),
                user_id=row.id,
                expires_at=int(time.time()) + 900,
                credential_version=credential.version,
            )
        )
        await session.flush()
        return proof, principal
    return await issue_session(session, manager, row)


async def authenticate_session(manager, token: str) -> Principal | None:
    """Validate durable session and active user on EVERY backend hop (no TTL cache)."""
    from config.auth_mode import google_login_enabled, local_auth_enabled
    from db import engine

    payload = manager.verify_token(token)
    if not payload or payload.get("token_use") != "session" or not payload.get("sid"):
        return None
    engine.init_engine()
    assert engine.SessionLocal is not None
    async with engine.SessionLocal() as session:
        record = await session.get(AuthSession, payload["sid"])
        if (
            record is None
            or record.expires_at <= time.time()
            or record.user_id != payload.get("sub")
        ):
            return None
        row = await session.get(User, record.user_id)
        if row is None or not row.is_active:
            return None
        if row.oauth_provider == "local":
            if not local_auth_enabled():
                return None
            credential = await session.get(LocalCredential, row.id)
            if (
                credential is None
                or credential.must_change_password
                or credential.version != record.credential_version
            ):
                return None
        elif row.oauth_provider == "google":
            if not google_login_enabled():
                return None
        else:
            return None
        principal = principal_from_row(row)
        principal.session_id = record.id
        principal.jwt_token = token
        manager.users[row.id] = principal
        return principal


async def revoke_sessions(session: AsyncSession, user_id: str) -> None:
    await session.execute(delete(AuthSession).where(col(AuthSession.user_id) == user_id))


async def reset_password(
    session: AsyncSession,
    user_id: str,
    password: str,
    actor_id: str,
    *,
    require_password_change: bool = False,
    expected_version: int | None = None,
) -> None:
    credential = await session.get(LocalCredential, user_id)
    if credential is None:
        raise HTTPException(404, "Local account not found")
    encoded = await hash_password(password, temporary=require_password_change)
    statement = update(LocalCredential).where(col(LocalCredential.user_id) == user_id)
    if expected_version is not None:
        statement = statement.where(col(LocalCredential.version) == expected_version)
    result = await session.execute(
        statement.values(
            password_hash=encoded,
            version=col(LocalCredential.version) + 1,
            must_change_password=require_password_change,
        )
    )
    if cast(CursorResult, result).rowcount != 1:
        raise HTTPException(401, "Password replacement expired; sign in again")
    await revoke_sessions(session, user_id)
    await AuditRepo(session).write(
        event="user.password_reset",
        actor_user_id=actor_id,
        target_type="user",
        target_id=user_id,
    )


async def password_change_identity(session: AsyncSession, proof: str | None):
    """Validate the limited first-login proof without creating a request principal."""
    from config.auth_mode import local_auth_enabled

    if not local_auth_enabled():
        return None
    if not proof or len(proof) > 128:
        return None
    record = await session.get(AuthSession, hashlib.sha256(proof.encode()).hexdigest())
    if record is None or record.expires_at <= time.time():
        return None
    row = await session.get(User, record.user_id)
    credential = await session.get(LocalCredential, record.user_id)
    if (
        row is None
        or not row.is_active
        or credential is None
        or not credential.must_change_password
        or credential.version != record.credential_version
    ):
        return None
    return row, credential
