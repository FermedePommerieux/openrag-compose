"""Unit tests for the unified duplicate-filename policy."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.processors import (
    DUPLICATE_FILENAME_WARNING,
    DocumentFileProcessor,
    S3FileProcessor,
)
from models.tasks import FileTask, TaskStatus, UploadTask


@pytest.mark.asyncio
async def test_traditional_processor_duplicate_exists_no_replace():
    """A duplicate without replacement is skipped consistently, not failed."""
    mock_doc_service = MagicMock()
    mock_models_service = MagicMock()
    mock_session_manager = MagicMock()

    processor = DocumentFileProcessor(
        document_service=mock_doc_service,
        models_service=mock_models_service,
        owner_user_id="user-123",
        jwt_token="mock-token",
        replace_duplicates=False,
        session_manager=mock_session_manager,
    )

    # Assert that session_manager was set correctly on the processor
    assert processor.session_manager == mock_session_manager

    # Mock base class methods directly on the instance to ensure perfect isolation
    processor.check_filename_exists = AsyncMock(return_value=True)
    processor.delete_document_by_filename = AsyncMock()

    upload_task = UploadTask(task_id="task-123", total_files=1)
    file_task = FileTask(file_path="/tmp/test.txt", filename="test.txt")

    await processor.process_item(upload_task, "/tmp/test.txt", file_task)

    assert file_task.status == TaskStatus.SKIPPED
    assert file_task.error is None
    assert file_task.result == {
        "status": "skipped",
        "reason": "duplicate_filename",
        "warning": DUPLICATE_FILENAME_WARNING,
    }
    assert upload_task.failed_files == 0
    assert upload_task.successful_files == 1

    processor.check_filename_exists.assert_called_once()
    processor.delete_document_by_filename.assert_not_called()
    mock_session_manager.get_user_opensearch_client.assert_called_once_with(
        "user-123", "mock-token"
    )


def _build_s3_processor(replace_duplicates: bool) -> S3FileProcessor:
    document_service = MagicMock()
    document_service.session_manager = MagicMock()
    return S3FileProcessor(
        document_service,
        bucket="test-bucket",
        s3_client=MagicMock(),
        owner_user_id="user-123",
        jwt_token="mock-token",
        models_service=MagicMock(),
        docling_service=MagicMock(),
        replace_duplicates=replace_duplicates,
    )


@pytest.mark.asyncio
async def test_s3_processor_duplicate_exists_no_replace():
    processor = _build_s3_processor(replace_duplicates=False)
    processor.check_filename_exists = AsyncMock(return_value=True)
    processor.delete_document_by_filename = AsyncMock()
    upload_task = UploadTask(task_id="task-s3", total_files=1)
    file_task = FileTask(file_path="docs/report.pdf", filename="docs/report.pdf")

    await processor.process_item(upload_task, "docs/report.pdf", file_task)

    assert file_task.status == TaskStatus.SKIPPED
    assert file_task.result["reason"] == "duplicate_filename"
    assert upload_task.successful_files == 1
    processor.s3_client.download_fileobj.assert_not_called()


@pytest.mark.asyncio
async def test_s3_processor_duplicate_exists_with_replace():
    processor = _build_s3_processor(replace_duplicates=True)
    processor.check_filename_exists = AsyncMock(return_value=True)
    processor.delete_document_by_filename = AsyncMock(return_value=1)
    processor.process_document_standard = AsyncMock(
        return_value={"status": "indexed", "id": "hash-1"}
    )
    processor.s3_client.head_object = MagicMock(return_value={"ContentLength": 10})
    upload_task = UploadTask(task_id="task-s3", total_files=1)
    file_task = FileTask(file_path="docs/report.pdf", filename="docs/report.pdf")

    with patch("models.processors.hash_id", return_value="dummy-hash"):
        await processor.process_item(upload_task, "docs/report.pdf", file_task)

    assert file_task.status == TaskStatus.COMPLETED
    assert upload_task.successful_files == 1
    assert processor.delete_document_by_filename.await_args.kwargs["owner_user_id"] == "user-123"
    processor.s3_client.download_fileobj.assert_called_once()
    processor.process_document_standard.assert_awaited_once()


@pytest.mark.asyncio
async def test_s3_processor_without_duplicate_proceeds():
    processor = _build_s3_processor(replace_duplicates=False)
    processor.check_filename_exists = AsyncMock(return_value=False)
    processor.delete_document_by_filename = AsyncMock()
    processor.process_document_standard = AsyncMock(
        return_value={"status": "indexed", "id": "hash-1"}
    )
    processor.s3_client.head_object = MagicMock(return_value={"ContentLength": 10})
    upload_task = UploadTask(task_id="task-s3", total_files=1)
    file_task = FileTask(file_path="docs/report.pdf", filename="docs/report.pdf")

    with patch("models.processors.hash_id", return_value="dummy-hash"):
        await processor.process_item(upload_task, "docs/report.pdf", file_task)

    assert file_task.status == TaskStatus.COMPLETED
    processor.delete_document_by_filename.assert_not_awaited()
    processor.process_document_standard.assert_awaited_once()


@pytest.mark.asyncio
async def test_traditional_processor_duplicate_exists_with_replace():
    """Verify that if a duplicate file exists and replace_duplicates is True, the old document is deleted and ingestion succeeds."""
    mock_doc_service = MagicMock()
    mock_models_service = MagicMock()
    mock_session_manager = MagicMock()

    processor = DocumentFileProcessor(
        document_service=mock_doc_service,
        models_service=mock_models_service,
        owner_user_id="user-123",
        jwt_token="mock-token",
        replace_duplicates=True,
        session_manager=mock_session_manager,
    )

    # Assert that session_manager was set correctly on the processor
    assert processor.session_manager == mock_session_manager

    # Mock base class methods directly on the instance to ensure perfect isolation
    processor.check_filename_exists = AsyncMock(return_value=True)
    processor.delete_document_by_filename = AsyncMock(return_value=1)
    processor.process_document_standard = AsyncMock(return_value={"status": "indexed"})

    upload_task = UploadTask(task_id="task-123", total_files=1)
    file_task = FileTask(file_path="/tmp/test.txt", filename="test.txt")

    with (
        patch("os.path.getsize", return_value=1234),
        patch("models.processors.hash_id", return_value="dummy-hash"),
    ):
        await processor.process_item(upload_task, "/tmp/test.txt", file_task)

    assert file_task.status == TaskStatus.COMPLETED
    assert file_task.error is None
    assert upload_task.failed_files == 0
    assert upload_task.successful_files == 1

    processor.check_filename_exists.assert_called_once()
    processor.delete_document_by_filename.assert_awaited_once()
    delete_call = processor.delete_document_by_filename.await_args
    assert delete_call.args[0] == "test.txt"
    assert delete_call.kwargs["owner_user_id"] == "user-123"
    assert delete_call.kwargs["shared"] is False
    processor.process_document_standard.assert_called_once()
    mock_session_manager.get_user_opensearch_client.assert_called_once_with(
        "user-123", "mock-token"
    )
