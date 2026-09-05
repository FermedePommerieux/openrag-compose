"""Operator-only local bootstrap/recovery: python -m auth.local_admin."""

import bootstrap  # noqa: F401 — load the backend environment before database settings

import argparse
import asyncio
import getpass

from sqlalchemy import select

from config.auth_mode import validate_auth_configuration
from db import engine
from db.migrations_runtime import run_alembic_upgrade_async
from db.models import LocalCredential, MigrationStatus
from db.seed import seed_roles_and_permissions
from services.local_auth_service import create_local_user, normalize_login, reset_password

BOOTSTRAP_MARKER = "local_admin_bootstrap_v1"


async def bootstrap_admin(session, login: str, password: str):
    if await session.get(MigrationStatus, BOOTSTRAP_MARKER) is not None:
        raise ValueError(
            "Local administrator bootstrap already completed; use recovery or admin APIs"
        )
    if (await session.execute(select(LocalCredential).limit(1))).first():
        raise ValueError("Local accounts already exist; use recovery or admin APIs")
    # Unique marker and credential creation share one transaction (race-safe).
    session.add(MigrationStatus(name=BOOTSTRAP_MARKER))
    await session.flush()
    return await create_local_user(session, login=login, password=password, role="admin")


async def run(action: str, login: str, password: str) -> str:
    validate_auth_configuration()
    await run_alembic_upgrade_async()
    engine.init_engine()
    assert engine.SessionLocal is not None
    try:
        async with engine.SessionLocal() as session:
            await seed_roles_and_permissions(session)
            if action == "bootstrap":
                row = await bootstrap_admin(session, login, password)
                user_id = row.id
            else:
                from sqlmodel import col

                credential = (
                    await session.execute(
                        select(LocalCredential).where(
                            col(LocalCredential.login) == normalize_login(login)
                        )
                    )
                ).scalar_one_or_none()
                if credential is None:
                    raise ValueError("Local account not found")
                user_id = credential.user_id
                await reset_password(session, user_id, password, user_id)
            await session.commit()
            return user_id
    finally:
        await engine.dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "reset-password"])
    parser.add_argument("login")
    args = parser.parse_args()
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Confirm password: "):
        parser.error("Passwords do not match")
    try:
        print("User ID:", asyncio.run(run(args.action, args.login, password)))
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
