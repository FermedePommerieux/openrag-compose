"""
Unit tests for services/docling_service.py
Validates async conversion logic, polling behavior, and error handling.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.docling_service import (
    DoclingServeError,
    DoclingService,
    PictureDescriptionConfigurationError,
    get_docling_preset_configs,
    get_picture_description_options,
)


def _make_response(status_code: int, json_data: dict = None) -> MagicMock:
    """Create a mock HTTP response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}

    # Mock raise_for_status to raise if status_code >= 400
    if status_code >= 400:

        def raise_status():
            raise httpx.HTTPStatusError("Error", request=MagicMock(), response=resp)

        resp.raise_for_status.side_effect = raise_status
    else:
        resp.raise_for_status.return_value = None

    return resp


@pytest.fixture
def mock_httpx_client():
    """Provide a mocked httpx AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    # Mock __aenter__ and __aexit__ for 'async with client' support
    client.__aenter__.return_value = client
    return client


@pytest.fixture
def docling_service(mock_httpx_client):
    """Provide a DoclingService instance with a mocked client."""
    return DoclingService(docling_url="http://docling:8000", httpx_client=mock_httpx_client)


@pytest.fixture(autouse=True)
def no_sleep():
    """Patch asyncio.sleep so tests run instantly."""
    with patch("services.docling_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        yield mock_sleep


# ── Polling Logic ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_result_success(docling_service, mock_httpx_client):
    """Returns json_content when Docling returns 'success'."""
    # First call: status poll -> success
    # Second call: get result -> document data
    mock_httpx_client.get.side_effect = [
        _make_response(200, {"task_status": "success"}),
        _make_response(200, {"document": {"json_content": {"key": "value"}}}),
    ]

    result = await docling_service._poll_result(mock_httpx_client, "task123", 1.0, 10.0)
    assert result == {"key": "value"}
    assert mock_httpx_client.get.call_count == 2
    # Verify URLs
    calls = mock_httpx_client.get.call_args_list
    assert calls[0].args[0].endswith("/v1/status/poll/task123")
    assert calls[1].args[0].endswith("/v1/result/task123")


@pytest.mark.asyncio
async def test_poll_result_waits_for_pending(docling_service, mock_httpx_client, no_sleep):
    """Polls multiple times if status is 'pending' before succeeding."""
    mock_httpx_client.get.side_effect = [
        _make_response(200, {"task_status": "pending"}),
        _make_response(200, {"task_status": "pending"}),
        _make_response(200, {"task_status": "success"}),
        _make_response(200, {"document": {"json_content": {"key": "value"}}}),
    ]

    result = await docling_service._poll_result(mock_httpx_client, "task123", 1.0, 10.0)
    assert result == {"key": "value"}
    assert mock_httpx_client.get.call_count == 4
    assert no_sleep.call_count == 2


@pytest.mark.asyncio
async def test_poll_result_failure_status(docling_service, mock_httpx_client):
    """Raises DoclingServeError when status is 'failure'."""
    mock_httpx_client.get.return_value = _make_response(200, {"task_status": "failure"})

    with pytest.raises(DoclingServeError, match="Docling processing failed"):
        await docling_service._poll_result(mock_httpx_client, "task123", 1.0, 10.0)


@pytest.mark.asyncio
async def test_poll_result_timeout(docling_service, mock_httpx_client, no_sleep):
    """Raises TimeoutError when task stays pending beyond timeout."""
    mock_httpx_client.get.return_value = _make_response(200, {"task_status": "pending"})

    # timeout=2.0, interval=1.0 -> loop runs at T=0, T=1, exits at T=2
    with pytest.raises(TimeoutError, match="did not complete within"):
        await docling_service._poll_result(mock_httpx_client, "task123", 1.0, 2.0)

    assert mock_httpx_client.get.call_count == 2
    assert no_sleep.call_count == 2


@pytest.mark.asyncio
async def test_poll_result_missing_content(docling_service, mock_httpx_client):
    """Raises DoclingServeError if result response is missing json_content."""
    mock_httpx_client.get.side_effect = [
        _make_response(200, {"task_status": "success"}),
        _make_response(200, {"document": {}}),  # No json_content
    ]

    with pytest.raises(DoclingServeError, match="missing document.json_content"):
        await docling_service._poll_result(mock_httpx_client, "task123", 1.0, 10.0)


@pytest.mark.asyncio
async def test_poll_result_http_error(docling_service, mock_httpx_client):
    """Propagates HTTP errors during polling as DoclingServeError."""
    mock_httpx_client.get.return_value = _make_response(500)

    with pytest.raises(DoclingServeError, match="Error polling docling status"):
        await docling_service._poll_result(mock_httpx_client, "task123", 1.0, 10.0)


# ── Upload Logic ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_success(docling_service, mock_httpx_client):
    """Returns task_id on successful upload."""
    mock_httpx_client.post.return_value = _make_response(200, {"task_id": "new-task-id"})

    # Mock config to avoid missing attribute errors during _build_docling_options
    with patch("services.docling_service.get_openrag_config") as mock_get_config:
        mock_config = MagicMock()
        mock_config.knowledge.table_structure = False
        mock_config.knowledge.ocr = False
        mock_config.knowledge.picture_descriptions = False
        mock_get_config.return_value = mock_config

        task_id = await docling_service.upload_to_docling_direct_async("test.pdf", b"data")

    assert task_id == "new-task-id"
    assert mock_httpx_client.post.call_count == 1

    # Verify boolean serialization (bool -> "true"/"false")
    _, kwargs = mock_httpx_client.post.call_args
    data = kwargs.get("data", {})
    assert data["do_ocr"] == "false"
    assert "ocr_preset" not in data
    assert "ocr_engine" not in data
    assert "ocr_custom_config" not in data


@pytest.mark.asyncio
async def test_upload_ocr_enabled_sends_rapidocr_preset(docling_service, mock_httpx_client):
    """OCR jobs request the administrator-owned RapidOCR preset."""
    mock_httpx_client.post.return_value = _make_response(200, {"task_id": "rapidocr-task"})
    mock_config = MagicMock()
    mock_config.knowledge.table_structure = True
    mock_config.knowledge.ocr = True
    mock_config.knowledge.picture_descriptions = False

    with patch("services.docling_service.get_openrag_config", return_value=mock_config):
        task_id = await docling_service.upload_to_docling_direct_async("test.pdf", b"data")

    assert task_id == "rapidocr-task"
    _, kwargs = mock_httpx_client.post.call_args
    data = kwargs["data"]
    assert data["do_ocr"] == "true"
    assert data["ocr_preset"] == "rapidocr"
    assert "ocr_engine" not in data
    assert "ocr_custom_config" not in data
    assert "ocr_lang" not in data


@pytest.mark.asyncio
async def test_upload_http_error(docling_service, mock_httpx_client):
    """Raises exception if upload returns non-200."""
    mock_httpx_client.post.return_value = _make_response(400)
    mock_config = MagicMock()
    mock_config.knowledge.table_structure = False
    mock_config.knowledge.ocr = False
    mock_config.knowledge.picture_descriptions = False

    with patch("services.docling_service.get_openrag_config", return_value=mock_config):
        with pytest.raises(httpx.HTTPStatusError):
            await docling_service.upload_to_docling_direct_async("test.pdf", b"data")


# ── Configuration Logic ─────────────────────────────────────────────


def test_build_docling_options_toggles(docling_service):
    """Correctly maps OpenRAG config to Docling options."""
    mock_config = MagicMock()
    mock_config.knowledge.table_structure = True
    mock_config.knowledge.ocr = True
    mock_config.knowledge.picture_descriptions = False

    with patch("services.docling_service.get_openrag_config", return_value=mock_config):
        options = docling_service._build_docling_options()

    assert options["do_table_structure"] is True
    assert options["do_ocr"] is True
    assert options["ocr_preset"] == "rapidocr"
    assert "ocr_engine" not in options
    assert "ocr_custom_config" not in options
    assert options["do_picture_description"] is False
    assert options["to_formats"] == "json"


def test_picture_descriptions_fail_before_submission_without_explicit_vlm(docling_service):
    """The feature toggle must never trigger Docling's implicit local model."""
    mock_config = MagicMock()
    mock_config.knowledge.table_structure = True
    mock_config.knowledge.ocr = True
    mock_config.knowledge.picture_descriptions = True

    with (
        patch("services.docling_service.get_openrag_config", return_value=mock_config),
        patch.dict("services.docling_service.os.environ", {}, clear=True),
        pytest.raises(PictureDescriptionConfigurationError, match="explicitly configured VLM"),
    ):
        docling_service._build_docling_options()


def test_remote_picture_description_options_are_explicit_and_authenticated():
    """Build Docling's OpenAI-compatible API option without exposing a default model."""
    mock_config = MagicMock()
    mock_config.providers.openai.api_key = "provider-key"

    with patch.dict(
        "services.docling_service.os.environ",
        {
            "OPENRAG_PICTURE_DESCRIPTION_API_URL": (
                "https://api.openai.com/v1/chat/completions"
            ),
            "OPENRAG_PICTURE_DESCRIPTION_MODEL": "gpt-5.6-sol",
        },
        clear=True,
    ):
        options = get_picture_description_options(mock_config)

    remote = options["picture_description_api"]
    assert remote["url"] == "https://api.openai.com/v1/chat/completions"
    assert remote["params"] == {"model": "gpt-5.6-sol"}
    assert remote["headers"] == {"Authorization": "Bearer provider-key"}


@pytest.mark.asyncio
async def test_upload_serializes_remote_picture_description_options(
    docling_service, mock_httpx_client
):
    """Nested remote VLM options are JSON multipart fields accepted by Docling Serve."""
    mock_httpx_client.post.return_value = _make_response(200, {"task_id": "vlm-task"})
    mock_config = MagicMock()
    mock_config.knowledge.table_structure = True
    mock_config.knowledge.ocr = True
    mock_config.knowledge.picture_descriptions = True
    mock_config.providers.openai.api_key = "provider-key"

    with (
        patch("services.docling_service.get_openrag_config", return_value=mock_config),
        patch.dict(
            "services.docling_service.os.environ",
            {
                "OPENRAG_PICTURE_DESCRIPTION_API_URL": "https://vlm.example/v1/chat/completions",
                "OPENRAG_PICTURE_DESCRIPTION_MODEL": "vision-model",
            },
            clear=True,
        ),
    ):
        task_id = await docling_service.upload_to_docling_direct_async("test.pdf", b"data")

    assert task_id == "vlm-task"
    data = mock_httpx_client.post.call_args.kwargs["data"]
    remote = json.loads(data["picture_description_api"])
    assert remote["params"] == {"model": "vision-model"}


def test_preset_configs_ocr_uses_rapidocr_independently_of_host_platform():
    """OCR selection is part of the job, not the OpenRAG host platform."""
    preset = get_docling_preset_configs(ocr=True)

    assert preset["do_ocr"] is True
    assert preset["ocr_preset"] == "rapidocr"
    assert "ocr_engine" not in preset
    assert "ocr_custom_config" not in preset
    assert "ocr_lang" not in preset


def test_preset_configs_ocr_disabled_omits_ocr_preset():
    """Disabling OCR preserves the toggle and does not force an OCR engine."""
    preset = get_docling_preset_configs(ocr=False)

    assert preset["do_ocr"] is False
    assert "ocr_preset" not in preset
    assert "ocr_custom_config" not in preset
    assert "ocr_engine" not in preset


def test_init_default_url():
    """Uses DOCLING_SERVE_URL from config.settings if not provided."""
    with patch("services.docling_service.DOCLING_SERVE_URL", "http://default:5001"):
        service = DoclingService()
        assert service.docling_url == "http://default:5001"
