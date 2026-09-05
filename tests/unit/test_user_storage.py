"""Two real local principals keep separate filesystem roots and DLS downloads."""

import secrets
from unittest.mock import AsyncMock

import pytest

from db.models import SourceArchiveLocation
from services.local_auth_service import create_local_user
from services.local_source_service import (
    LocalSourceNotFoundError,
    delete_local_source,
    get_local_source_archive_stats,
    resolve_ingestion_path,
    resolve_local_source_download,
    stage_local_source,
)
from services.user_storage_service import get_user_storage
from tests.unit import test_local_auth as shared_auth

local_stack = shared_auth.local_stack


@pytest.mark.asyncio
async def test_user_ingestion_and_archives_are_confined_and_links_are_authorized(
    local_stack, monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(tmp_path / "documents"))
    stack = local_stack
    async with stack.factory() as session:
        alice = await create_local_user(session, login="alice", password=secrets.token_urlsafe(24))
        bob = await create_local_user(session, login="bob", password=secrets.token_urlsafe(24))
        await session.commit()
    a = await get_user_storage(alice.id)
    b = await get_user_storage(bob.id)
    assert a.ingestion == tmp_path / "documents/alice/ingestion"
    assert a.archive == tmp_path / "documents/alice/archives"
    assert resolve_ingestion_path(str(b.ingestion), ingestion_root=a.ingestion) is None
    assert resolve_ingestion_path("../../bob/ingestion", ingestion_root=a.ingestion) is None
    (a.ingestion / "escape").symlink_to(b.ingestion, target_is_directory=True)
    assert resolve_ingestion_path("escape", ingestion_root=a.ingestion) is None
    assert resolve_ingestion_path() is None
    source = a.ingestion / "note.txt"
    source.write_text("Private original")
    staged = await stage_local_source(source, "a" * 24, source.name, owner_user_id=alice.id)
    assert staged.archived_path.is_relative_to(a.archive)
    assert not source.exists()
    async with stack.factory() as session:
        assert (await session.get(SourceArchiveLocation, staged.source_id)).user_id == alice.id
    allowed = AsyncMock()
    allowed.search.return_value = {"hits": {"total": {"value": 1}}}
    denied = AsyncMock()
    denied.search.return_value = {"hits": {"total": {"value": 0}}}
    with pytest.raises(LocalSourceNotFoundError):
        await resolve_local_source_download(
            staged.source_id, opensearch_client=denied, index="test-docs"
        )
    # A shared reader uses the same authorized source URL; no owner-directory input is accepted.
    result = await resolve_local_source_download(
        staged.source_id, opensearch_client=allowed, index="test-docs"
    )
    assert result.path.read_text() == "Private original"
    assert get_local_source_archive_stats(storage=a)["used_bytes"] == len("Private original")
    assert get_local_source_archive_stats(storage=b)["used_bytes"] == 0
    await staged.rollback()
    assert source.read_text() == "Private original"
    async with stack.factory() as session:
        assert await session.get(SourceArchiveLocation, staged.source_id) is None
    staged = await stage_local_source(source, "a" * 24, source.name, owner_user_id=alice.id)
    staged.commit()
    assert await delete_local_source(staged.source_id)
    assert not staged.archived_path.exists()
    assert b.ingestion.exists() and b.archive.exists()


@pytest.mark.asyncio
async def test_storage_root_symlink_cannot_redirect_into_another_account(
    local_stack, monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(tmp_path))
    (tmp_path / "operator").symlink_to(tmp_path / "someone-else", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        await get_user_storage(local_stack.admin_id)


@pytest.mark.asyncio
async def test_real_path_api_only_schedules_its_authenticated_owner(
    local_stack, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from api import upload

    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(tmp_path / "documents"))
    monkeypatch.setattr("api.documents._ensure_index_exists", AsyncMock())
    stack = local_stack
    stack.app.state.services["task_service"] = SimpleNamespace(
        create_upload_task=AsyncMock(return_value="test-task")
    )
    stack.app.add_api_route("/upload-path", upload.upload_path, methods=["POST"])
    stack.app.add_api_route("/upload-options", upload.upload_options, methods=["GET"])
    own = await get_user_storage(stack.admin_id)
    async with stack.factory() as session:
        other = await create_local_user(
            session, login="other-user", password=secrets.token_urlsafe(24)
        )
        await session.commit()
    foreign = await get_user_storage(other.id)
    (foreign.ingestion / "secret.txt").write_text("other user bytes")
    (own.ingestion / "owned.txt").write_text("owned bytes")
    result = await stack.client.get("/upload-options")
    assert result.json()["documents_path"] == str(own.ingestion)
    assert (
        await stack.client.post("/upload-path", json={"path": str(foreign.ingestion)})
    ).status_code == 400
    assert (
        await stack.client.post("/upload-path", json={"path": "../../other-user/ingestion"})
    ).status_code == 400
    result = await stack.client.post(
        "/upload-path", json={"path": "owned.txt", "archive_sources": True, "owner": other.id}
    )
    assert result.status_code == 201
    task = stack.app.state.services["task_service"].create_upload_task
    assert task.await_args.args[0] == stack.admin_id
    assert task.await_args.args[1] == [str(own.ingestion / "owned.txt")]
    assert task.await_args.kwargs["archive_sources"] is True


@pytest.mark.asyncio
async def test_new_account_cannot_claim_preexisting_legacy_directory(
    local_stack, monkeypatch, tmp_path
):
    from db.models import UserStorage

    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(tmp_path / "documents"))
    legacy = tmp_path / "documents/legacy/ingestion"
    legacy.mkdir(parents=True)
    (legacy / "existing.txt").write_text("Legacy owner data")
    async with local_stack.factory() as session:
        user = await create_local_user(session, login="legacy", password=secrets.token_urlsafe(24))
        await session.commit()
    with pytest.raises(ValueError, match="explicit ownership migration"):
        await get_user_storage(user.id)
    async with local_stack.factory() as session:
        assert await session.get(UserStorage, user.id) is None
    assert (legacy / "existing.txt").read_text() == "Legacy owner data"
