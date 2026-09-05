"""One-time browser enrollment, attached to the existing bootstrap transaction."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from config.auth_mode import (
    get_deployment_auth_mode,
    set_onboarding_local_auth,
    validate_local_auth_prerequisites,
)
from db.models import LocalCredential, MigrationStatus, User
from db.repositories.workspace_config_repo import WorkspaceConfigRepo

SETUP_MARKER = "local_auth_browser_setup_v1"
ADMIN_MARKER = "local_admin_bootstrap_v1"


async def load_onboarding_auth_policy() -> None:
    """Load before service initialization; DB failures must never enable no-auth."""
    from db import engine

    if get_deployment_auth_mode() != "auto":
        set_onboarding_local_auth(False)
        return
    engine.init_engine()
    assert engine.SessionLocal is not None
    async with engine.SessionLocal() as session:
        marker = await session.get(MigrationStatus, SETUP_MARKER)
        if marker is not None and marker.notes not in {"local", "skipped", "closed"}:
            raise RuntimeError("Invalid persisted authentication setup choice")
        if marker is None and await _has_existing_setup(session):
            # Preserve the upgrade boundary even if the old provider wizard is
            # reset later. An established installation uses operator bootstrap.
            marker = MigrationStatus(name=SETUP_MARKER, notes="closed")
            session.add(marker)
            await session.commit()
        set_onboarding_local_auth(marker is not None and marker.notes == "local")


async def local_setup_status(session: AsyncSession) -> dict[str, bool]:
    mode = get_deployment_auth_mode()
    unavailable = {"local_setup_available": False, "local_setup_can_skip": False}
    if mode not in {"auto", "local", "local_plus_external"}:
        return unavailable
    try:
        validate_local_auth_prerequisites()
    except RuntimeError:
        return unavailable
    if await session.get(MigrationStatus, SETUP_MARKER) or await _has_existing_setup(session):
        return unavailable
    return {"local_setup_available": True, "local_setup_can_skip": mode == "auto"}


async def _has_existing_setup(session: AsyncSession) -> bool:
    from config.settings import get_openrag_config

    if await session.get(MigrationStatus, ADMIN_MARKER):
        return True
    if (await session.execute(select(LocalCredential).limit(1))).first():
        return True
    # Only the exact legacy anonymous principal is permitted before setup.
    if (
        await session.execute(
            select(User)
            .where(
                or_(
                    col(User.id) != "anonymous",
                    col(User.oauth_provider) != "none",
                    col(User.oauth_subject) != "anonymous",
                )
            )
            .limit(1)
        )
    ).first():
        return True
    sections = await WorkspaceConfigRepo(session).list_all()
    onboarding = sections.get("onboarding") or {}
    config = get_openrag_config()
    if (sections.get("meta") or {}).get("edited") or config.edited:
        return True
    return onboarding.get("current_step", 0) not in (0, None) or config.onboarding.current_step != 0
