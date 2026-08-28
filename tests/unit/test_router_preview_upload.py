"""Tests that upload options are threaded through the upload router."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import UploadFile

from api.router import (
    _folder_upload_provenances,
    _langflow_upload_ingest_task,
    _normalize_source_provenances,
    _normalize_source_urls,
    _resolve_archive_source,
    _traditional_upload_ingest_task,
    upload_ingest_router,
)
from session_manager import User


@pytest.mark.asyncio
async def test_langflow_upload_passes_preview_mode_to_task_service():
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = "sample.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(return_value=b"%PDF-sample")

    mock_task_service = MagicMock()
    mock_task_service.create_langflow_upload_task = AsyncMock(return_value="task-preview-1")

    user = User(user_id="user-1", email="u@example.com", name="User", jwt_token="Bearer tok")

    mock_temp_file = MagicMock()
    mock_temp_file.name = "/tmp/sample.pdf"

    with (
        patch("api.router.tempfile.NamedTemporaryFile", return_value=mock_temp_file),
        patch("api.router.open", create=True),
        patch("utils.file_utils.safe_unlink"),
        patch("api.router.is_ingest_preview_enabled", return_value=True),
    ):
        response = await _langflow_upload_ingest_task(
            upload_files=[mock_file],
            session_id=None,
            settings_json=None,
            tweaks_json=None,
            replace_duplicates=True,
            create_filter=False,
            preview_mode=True,
            source_urls=["https://files.example.com/sample.pdf"],
            source_provenances=[
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "entity": {
                            "id": "urn:openrag:document:sample",
                            "type": "http://www.w3.org/ns/prov#Entity",
                        },
                    }
                )
            ],
            archive_sources=True,
            langflow_file_service=MagicMock(),
            session_manager=MagicMock(),
            task_service=mock_task_service,
            user=user,
        )

    assert response.status_code == 202
    call_kwargs = mock_task_service.create_langflow_upload_task.await_args.kwargs
    assert call_kwargs["preview_mode"] is True
    assert call_kwargs["source_urls"] == {"/tmp/sample.pdf": "https://files.example.com/sample.pdf"}
    assert call_kwargs["source_provenances"]["/tmp/sample.pdf"].entity.id == (
        "urn:openrag:document:sample"
    )
    assert call_kwargs["archive_sources"] is True

    body = json.loads(response.body.decode())
    assert body["preview_mode"] is True


@pytest.mark.asyncio
async def test_browser_folder_upload_forwards_tree_and_disables_filename_dedupe():
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = "agreement.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(return_value=b"%PDF-sample")
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="task-folder-1")
    temp_file = MagicMock()
    temp_file.name = "/tmp/agreement.pdf"

    with (
        patch("api.router.tempfile.NamedTemporaryFile", return_value=temp_file),
        patch("api.router.open", create=True),
        patch("utils.file_utils.safe_unlink"),
        patch("api.documents._ensure_index_exists", new=AsyncMock()),
    ):
        response = await _traditional_upload_ingest_task(
            upload_files=[mock_file],
            replace_duplicates=False,
            create_filter=False,
            preview_mode=False,
            session_manager=MagicMock(),
            task_service=task_service,
            user=User(
                user_id="user-1",
                email="u@example.com",
                name="User",
                jwt_token="Bearer tok",
            ),
            source_relative_paths=["contracts/2024/agreement.pdf"],
            source_collection_label="project",
        )

    assert response.status_code == 202
    kwargs = task_service.create_upload_task.await_args.kwargs
    provenance = kwargs["source_provenances"]["/tmp/agreement.pdf"]
    assert provenance.relative_path == "contracts/2024/agreement.pdf"
    assert kwargs["dedupe_by_filename"] is False


@pytest.mark.asyncio
async def test_upload_ingest_router_ignores_preview_when_disabled():
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = "sample.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(return_value=b"%PDF-sample")

    mock_task_service = MagicMock()
    mock_task_service.create_langflow_upload_task = AsyncMock(return_value="task-1")

    user = User(user_id="user-1", email="u@example.com", name="User", jwt_token="Bearer tok")

    mock_temp_file = MagicMock()
    mock_temp_file.name = "/tmp/sample.pdf"

    with (
        patch("api.router.get_openrag_config") as mock_cfg,
        patch("api.router.tempfile.NamedTemporaryFile", return_value=mock_temp_file),
        patch("api.router.open", create=True),
        patch("utils.file_utils.safe_unlink"),
        patch("api.router.is_ingest_preview_enabled", return_value=False),
        patch("config.settings.is_no_auth_mode", return_value=True),
    ):
        mock_cfg.return_value.knowledge.disable_ingest_with_langflow = False

        response = await upload_ingest_router(
            file=[mock_file],
            session_id=None,
            settings_json=None,
            tweaks_json=None,
            preview="true",
            replace_duplicates="true",
            create_filter="false",
            archive_source="true",
            langflow_file_service=MagicMock(),
            session_manager=MagicMock(),
            task_service=mock_task_service,
            document_service=MagicMock(),
            user=user,
        )

    assert response.status_code == 202
    call_kwargs = mock_task_service.create_langflow_upload_task.await_args.kwargs
    assert call_kwargs["preview_mode"] is False
    assert call_kwargs["archive_sources"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_urls", "expected_source_urls"),
    [
        (None, {}),
        (
            ["https://archive.example.com/original.pdf"],
            {"/tmp/sample.pdf": "https://archive.example.com/original.pdf"},
        ),
    ],
)
@pytest.mark.parametrize(
    ("disable_langflow_ingest", "task_method"),
    [
        (False, "create_langflow_upload_task"),
        (True, "create_upload_task"),
    ],
)
async def test_multi_user_upload_remains_available_without_local_archiving(
    source_urls,
    expected_source_urls,
    disable_langflow_ingest,
    task_method,
):
    """Keep file uploads available while disabling local archive storage."""
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = "sample.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(return_value=b"%PDF-sample")

    mock_task_service = MagicMock()
    mock_task_service.create_langflow_upload_task = AsyncMock(return_value="task-1")
    mock_task_service.create_upload_task = AsyncMock(return_value="task-1")
    mock_temp_file = MagicMock()
    mock_temp_file.name = "/tmp/sample.pdf"
    user = User(user_id="user-1", email="u@example.com", name="User", jwt_token="Bearer tok")

    with (
        patch("api.router.get_openrag_config") as mock_cfg,
        patch("api.router.tempfile.NamedTemporaryFile", return_value=mock_temp_file),
        patch("api.router.open", create=True),
        patch("utils.file_utils.safe_unlink"),
        patch("api.router.is_ingest_preview_enabled", return_value=False),
        patch("config.settings.is_no_auth_mode", return_value=False),
        patch("api.documents._ensure_index_exists", new=AsyncMock()),
    ):
        mock_cfg.return_value.knowledge.disable_ingest_with_langflow = disable_langflow_ingest
        response = await upload_ingest_router(
            file=[mock_file],
            session_id=None,
            settings_json=None,
            tweaks_json=None,
            replace_duplicates="true",
            create_filter="false",
            preview="false",
            source_url=source_urls,
            archive_source=None,
            langflow_file_service=MagicMock(),
            session_manager=MagicMock(),
            task_service=mock_task_service,
            document_service=MagicMock(),
            user=user,
        )

    assert response.status_code == 202
    call_kwargs = getattr(mock_task_service, task_method).await_args.kwargs
    assert call_kwargs["source_urls"] == expected_source_urls
    assert call_kwargs["archive_sources"] is False


@pytest.mark.asyncio
async def test_multi_user_upload_rejects_local_archive_request():
    """Reject an explicit local archive request in multi-user mode."""
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = "sample.pdf"
    mock_task_service = MagicMock()
    user = User(user_id="user-1", email="u@example.com", name="User", jwt_token="Bearer tok")

    with (
        patch("api.router.get_openrag_config") as mock_cfg,
        patch("config.settings.is_no_auth_mode", return_value=False),
    ):
        mock_cfg.return_value.knowledge.disable_ingest_with_langflow = False
        response = await upload_ingest_router(
            file=[mock_file],
            session_id=None,
            settings_json=None,
            tweaks_json=None,
            replace_duplicates="true",
            create_filter="false",
            preview="false",
            source_url=None,
            archive_source="true",
            langflow_file_service=MagicMock(),
            session_manager=MagicMock(),
            task_service=mock_task_service,
            document_service=MagicMock(),
            user=user,
        )

    assert response.status_code == 400
    mock_task_service.create_langflow_upload_task.assert_not_called()


def test_source_urls_must_be_http_and_match_uploaded_files():
    """Validate source URL count, protocol, credentials, and characters."""
    files = [MagicMock(spec=UploadFile), MagicMock(spec=UploadFile)]

    with pytest.raises(ValueError, match="once for each"):
        _normalize_source_urls(files, ["https://files.example.com/one.pdf"])

    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        _normalize_source_urls(files[:1], ["javascript:alert(1)"])

    with pytest.raises(ValueError, match="embedded credentials"):
        _normalize_source_urls(files[:1], ["https://user:secret@example.com/file.pdf"])

    with pytest.raises(ValueError, match="control characters"):
        _normalize_source_urls(files[:1], ["https://example.com/file\x7f.pdf"])


def test_source_provenance_is_optional_and_validated_as_json_per_file():
    files = [MagicMock(spec=UploadFile), MagicMock(spec=UploadFile)]
    payload = {
        "schema_version": "1.0",
        "entity": {
            "id": "urn:openrag:email:message-1",
            "type": "http://www.w3.org/ns/prov#Entity",
        },
    }

    assert _normalize_source_provenances(files, None) == [None, None]
    parsed = _normalize_source_provenances(files[:1], [json.dumps(payload)])
    assert parsed[0].entity.id == "urn:openrag:email:message-1"

    with pytest.raises(ValueError, match="once for each"):
        _normalize_source_provenances(files, [json.dumps(payload)])
    with pytest.raises(ValueError, match="valid JSON"):
        _normalize_source_provenances(files[:1], ["{not-json"])


def test_source_provenance_rejects_unknown_json_fields():
    files = [MagicMock(spec=UploadFile)]
    payload = {
        "schema_version": "1.0",
        "entity": {
            "id": "urn:openrag:email:message-1",
            "type": "http://www.w3.org/ns/prov#Entity",
        },
        "unexpected": "not part of the bounded contract",
    }

    with pytest.raises(ValueError, match="invalid source_provenance"):
        _normalize_source_provenances(files, [json.dumps(payload)])


def test_browser_folder_paths_create_linked_provenance():
    files = [MagicMock(spec=UploadFile), MagicMock(spec=UploadFile)]

    provenances = _folder_upload_provenances(
        files,
        ["contracts/2024/agreement.pdf", "letters/reply.pdf"],
        "project",
        "user-1",
        [None, None],
    )

    first, second = provenances
    assert first is not None
    assert second is not None
    assert first.relative_path == "contracts/2024/agreement.pdf"
    assert second.relative_path == "letters/reply.pdf"
    assert first.entity.source_system == "browser_folder"
    assert first.relations[0].target.id == second.relations[0].target.id


@pytest.mark.parametrize(
    ("relative_paths", "collection_label", "error"),
    [
        (["one.pdf"], "project", "once for each"),
        (["../one.pdf", "two.pdf"], "project", "relative_path"),
        (["one.pdf", "two.pdf"], None, "required"),
        (["one.pdf", "two.pdf"], "../project", "portable folder"),
    ],
)
def test_browser_folder_provenance_rejects_incomplete_or_unsafe_context(
    relative_paths, collection_label, error
):
    files = [MagicMock(spec=UploadFile), MagicMock(spec=UploadFile)]

    with pytest.raises(ValueError, match=error):
        _folder_upload_provenances(
            files,
            relative_paths,
            collection_label,
            "user-1",
            [None, None],
        )


def test_browser_folder_hint_cannot_override_explicit_provenance():
    files = [MagicMock(spec=UploadFile)]
    explicit = _normalize_source_provenances(
        files,
        [
            json.dumps(
                {
                    "entity": {
                        "id": "urn:example:known-source",
                        "type": "document",
                    }
                }
            )
        ],
    )

    with pytest.raises(ValueError, match="cannot be combined"):
        _folder_upload_provenances(
            files,
            ["one.pdf"],
            "project",
            "user-1",
            explicit,
        )


def test_manual_upload_uses_global_archiving_setting_when_form_field_is_absent(
    monkeypatch,
):
    """Use the global archive setting when an upload has no override."""
    monkeypatch.setattr(
        "services.local_source_service.is_source_archiving_enabled",
        lambda: True,
    )

    assert _resolve_archive_source(None) is True
    assert _resolve_archive_source("false") is False


@pytest.mark.asyncio
async def test_hybrid_upload_uses_backend_pipeline_even_when_langflow_is_enabled():
    """HybridChunker lives in the backend pipeline, so the router must reach it."""
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = "hybrid.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(return_value=b"%PDF-hybrid")
    mock_temp_file = MagicMock()
    mock_temp_file.name = "/tmp/hybrid.pdf"
    task_service = MagicMock()
    task_service.create_upload_task = AsyncMock(return_value="hybrid-task")
    task_service.create_langflow_upload_task = AsyncMock(return_value="langflow-task")
    user = User(user_id="user-1", email="u@example.com", name="User", jwt_token="Bearer tok")

    with (
        patch("api.router.get_openrag_config") as mock_cfg,
        patch("api.router.tempfile.NamedTemporaryFile", return_value=mock_temp_file),
        patch("api.router.open", create=True),
        patch("utils.file_utils.safe_unlink"),
        patch("api.router.is_ingest_preview_enabled", return_value=False),
        patch("config.settings.is_no_auth_mode", return_value=True),
        patch("api.documents._ensure_index_exists", new=AsyncMock()),
    ):
        mock_cfg.return_value.knowledge.disable_ingest_with_langflow = False
        mock_cfg.return_value.knowledge.chunking_strategy = "hybrid"
        response = await upload_ingest_router(
            file=[mock_file],
            session_id=None,
            settings_json=None,
            tweaks_json=None,
            replace_duplicates="true",
            create_filter="false",
            preview="false",
            archive_source="false",
            langflow_file_service=MagicMock(),
            session_manager=MagicMock(),
            task_service=task_service,
            document_service=MagicMock(),
            user=user,
        )

    assert response.status_code == 202
    task_service.create_upload_task.assert_awaited_once()
    task_service.create_langflow_upload_task.assert_not_called()
