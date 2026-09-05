"""Resolve server-assigned user directories without using request-supplied paths."""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from config.settings import get_documents_path
from db import engine
from db.models import LocalCredential, SourceArchiveLocation, User, UserStorage

_STORAGE_LOCK = asyncio.Lock()
DIRECTORY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}")


@dataclass(frozen=True)
class UserStoragePaths:
    user_id: str
    directory: str
    root: Path
    ingestion: Path
    archive: Path


def storage_paths(row: UserStorage) -> UserStoragePaths:
    if not DIRECTORY_PATTERN.fullmatch(row.directory):
        raise ValueError("Invalid managed storage directory")
    base = Path(get_documents_path()).expanduser().resolve()
    root = base / row.directory
    for path in (root, root / "ingestion", root / "archives"):
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            raise ValueError("Managed storage cannot use a symlink or escape its root")
    return UserStoragePaths(row.user_id, row.directory, root, root / "ingestion", root / "archives")


async def ensure_storage_row(session: AsyncSession, user_id: str) -> UserStorage:
    existing = await session.get(UserStorage, user_id)
    if existing is not None:
        return existing
    user = await session.get(User, user_id)
    if user is None or not user.is_active or user.oauth_provider == "none":
        raise ValueError("An active application account is required for user storage")
    credential = await session.get(LocalCredential, user_id)
    directory = (
        credential.login
        if credential
        else "user-" + hashlib.sha256(user_id.encode()).hexdigest()[:32]
    )
    row = UserStorage(user_id=user_id, directory=directory)
    if storage_paths(row).root.exists():
        raise ValueError("Existing storage directory requires an explicit ownership migration")
    session.add(row)
    await session.flush()
    return row


async def get_user_storage(user_id: str, *, create_directories: bool = True) -> UserStoragePaths:
    engine.init_engine()
    assert engine.SessionLocal is not None
    async with _STORAGE_LOCK:
        async with engine.SessionLocal() as session:
            row = await ensure_storage_row(session, user_id)
            result = storage_paths(row)
            await session.commit()
        if create_directories:
            result.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            result.ingestion.mkdir(parents=True, exist_ok=True, mode=0o700)
            result.archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        return result


async def register_archive(source_id: str, user_id: str) -> None:
    engine.init_engine()
    assert engine.SessionLocal is not None
    async with engine.SessionLocal() as session:
        await ensure_storage_row(session, user_id)
        session.add(SourceArchiveLocation(source_id=source_id, user_id=user_id))
        await session.commit()


async def archive_root_for_source(source_id: str) -> Path | None:
    engine.init_engine()
    assert engine.SessionLocal is not None
    async with engine.SessionLocal() as session:
        location = await session.get(SourceArchiveLocation, source_id)
        if location is None:
            return None
        row = await session.get(UserStorage, location.user_id)
        if row is None:
            raise ValueError("Archive storage binding is missing")
        return storage_paths(row).archive


async def unregister_archive(source_id: str) -> None:
    engine.init_engine()
    assert engine.SessionLocal is not None
    async with engine.SessionLocal() as session:
        await session.execute(
            delete(SourceArchiveLocation).where(col(SourceArchiveLocation.source_id) == source_id)
        )
        await session.commit()
