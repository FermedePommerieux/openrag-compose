import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.upload import UploadPathBody, upload_options, upload_path
from api.v1.documents import IngestPathV1Body, ingest_path_endpoint
from session_manager import User


@pytest.mark.asyncio
async def test_upload_path_archives_sources_without_temporary_cleanup(tmp_path, monkeypatch):
    """Archive path-ingested sources without treating them as temporary files."""
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(tmp_path))
    monkeypatch.delenv("OPENRAG_INDEXED_DOCUMENTS_PATH", raising=False)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "message.eml"
    source.write_bytes(b"From: sender@example.com\n\nHello")

    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task-1")
    ensure_index = AsyncMock()
    monkeypatch.setattr("api.documents._ensure_index_exists", ensure_index)
    user = User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        jwt_token="Bearer token",
    )

    response = await upload_path(
        UploadPathBody(path=str(inbox), replace_duplicates=True, archive_sources=True),
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )

    assert response.status_code == 201
    assert json.loads(response.body)["archive_sources"] is True
    call = task_service.create_upload_task.await_args
    assert call.args[1] == [str(source.resolve())]
    kwargs = call.kwargs
    assert kwargs["replace_duplicates"] is True
    assert kwargs["archive_sources"] is True
    assert kwargs["cleanup_files"] is False
    assert kwargs["delete_source_after_success"] is True
    provenance = kwargs["source_provenances"][str(source.resolve())]
    assert provenance.relative_path == "message.eml"
    assert provenance.entity.type == "file"
    assert provenance.relations[0].role.value == "member_of"
    ensure_index.assert_awaited_once_with("Bearer token")


@pytest.mark.asyncio
async def test_v1_ingest_path_accepts_only_shared_documents(tmp_path, monkeypatch):
    """Confine public path ingestion to the shared documents directory."""
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    documents = tmp_path / "documents"
    source = documents / "inbox" / "message.eml"
    source.parent.mkdir(parents=True)
    source.write_text("From: sender@example.com\n\nHello")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(documents))

    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task-2")
    ensure_index = AsyncMock()
    monkeypatch.setattr("api.documents._ensure_index_exists", ensure_index)
    user = User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        jwt_token="Bearer token",
    )

    accepted = await ingest_path_endpoint(
        IngestPathV1Body(
            path="inbox/message.eml",
            replace_duplicates=True,
            archive_source=True,
        ),
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )
    rejected = await ingest_path_endpoint(
        IngestPathV1Body(path=str(outside)),
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )

    assert accepted.status_code == 201
    assert rejected.status_code == 400
    assert task_service.create_upload_task.await_count == 1


@pytest.mark.asyncio
async def test_internal_upload_path_is_also_confined_to_shared_documents(tmp_path, monkeypatch):
    """Confine internal path ingestion to the shared documents directory."""
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    documents = tmp_path / "documents"
    documents.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(documents))
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task")
    user = User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        jwt_token="Bearer token",
    )

    response = await upload_path(
        UploadPathBody(path=str(outside)),
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )

    assert response.status_code == 400
    task_service.create_upload_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_path_passes_structured_optional_provenance(tmp_path, monkeypatch):
    """Keep path API provenance as a typed JSON object, not a string field."""
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    documents = tmp_path / "documents"
    documents.mkdir()
    source = documents / "message.eml"
    source.write_text("Message-ID: <message-1@example.test>\n\nHello")
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(documents))
    monkeypatch.setattr("api.documents._ensure_index_exists", AsyncMock())
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task-prov")
    user = User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        jwt_token="Bearer token",
    )

    body = UploadPathBody.model_validate(
        {
            "path": str(source),
            "source_provenance": {
                "schema_version": "1.0",
                "entity": {
                    "id": "urn:openrag:email:message-1",
                    "type": "http://www.w3.org/ns/prov#Entity",
                },
            },
        }
    )
    response = await upload_path(
        body,
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )

    assert response.status_code == 201
    provenances = task_service.create_upload_task.await_args.kwargs["source_provenances"]
    provenance = provenances[str(source.resolve())]
    assert provenance.entity.id == "urn:openrag:email:message-1"
    assert provenance.relative_path == "message.eml"


@pytest.mark.asyncio
async def test_upload_path_links_folder_members_and_preserves_relative_tree(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    documents = tmp_path / "documents"
    project = documents / "project"
    first = project / "contracts" / "2024" / "agreement.pdf"
    second = project / "contracts" / "2025" / "renewal.pdf"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(documents))
    monkeypatch.setattr("api.documents._ensure_index_exists", AsyncMock())
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task-tree")

    response = await upload_path(
        UploadPathBody(path=str(project)),
        task_service=task_service,
        session_manager=MagicMock(),
        user=User(
            user_id="user-1",
            email="user@example.com",
            name="User",
            jwt_token="Bearer token",
        ),
    )

    assert response.status_code == 201
    provenances = task_service.create_upload_task.await_args.kwargs["source_provenances"]
    first_provenance = provenances[str(first.resolve())]
    second_provenance = provenances[str(second.resolve())]
    assert first_provenance.relative_path == "contracts/2024/agreement.pdf"
    assert second_provenance.relative_path == "contracts/2025/renewal.pdf"
    assert first_provenance.entity.id != second_provenance.entity.id
    assert first_provenance.relations[0].target.id == second_provenance.relations[0].target.id


@pytest.mark.asyncio
async def test_upload_path_rejects_one_provenance_for_a_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: True)
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "one.txt").write_text("one")
    (documents / "two.txt").write_text("two")
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(documents))
    monkeypatch.setattr("api.documents._ensure_index_exists", AsyncMock())
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="unexpected")

    response = await upload_path(
        UploadPathBody.model_validate(
            {
                "path": str(documents),
                "source_provenance": {
                    "schema_version": "1.0",
                    "entity": {
                        "id": "urn:openrag:collection:wrong-scope",
                        "type": "http://www.w3.org/ns/prov#Collection",
                    },
                },
            }
        ),
        task_service=task_service,
        session_manager=MagicMock(),
        user=User(
            user_id="user-1",
            email="user@example.com",
            name="User",
            jwt_token="Bearer token",
        ),
    )

    assert response.status_code == 400
    assert "exactly one file" in json.loads(response.body)["error"]
    task_service.create_upload_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_path_ingestion_is_disabled_in_multi_user_mode(tmp_path, monkeypatch):
    """Disable every local path ingestion surface in multi-user mode."""
    documents = tmp_path / "documents"
    source = documents / "message.eml"
    documents.mkdir()
    source.write_text("message")
    monkeypatch.setenv("OPENRAG_DOCUMENTS_PATH", str(documents))
    monkeypatch.setattr("config.settings.is_no_auth_mode", lambda: False)
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task")
    user = User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        jwt_token="Bearer token",
    )

    internal = await upload_path(
        UploadPathBody(path=str(source)),
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )
    public = await ingest_path_endpoint(
        IngestPathV1Body(path=str(source)),
        task_service=task_service,
        session_manager=MagicMock(),
        user=user,
    )
    options = await upload_options(user=user)

    assert internal.status_code == 403
    assert public.status_code == 403
    options_payload = json.loads(options.body)
    assert options_payload["local_path_ingestion_enabled"] is False
    assert "documents_path" not in options_payload
    task_service.create_upload_task.assert_not_awaited()
