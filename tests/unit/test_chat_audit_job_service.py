import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from services.chat_audit_job_service import ChatAuditJobService, _audit_error_message


def test_empty_timeout_error_is_persisted_with_a_useful_explanation() -> None:
    assert _audit_error_message(TimeoutError()) == (
        "Exhaustive audit timed out before source verification completed"
    )


@pytest.mark.asyncio
async def test_audit_producer_survives_subscriber_disconnect() -> None:
    release = asyncio.Event()

    async def stream():
        yield (
            json.dumps(
                {
                    "type": "openrag.audit.progress",
                    "progress": {"phase": "document_read", "complete": False},
                }
            )
            + "\n"
        ).encode()
        await release.wait()
        yield (json.dumps({"delta": {"content": "Verified answer"}}) + "\n").encode()
        yield (
            json.dumps(
                {
                    "type": "response.output_text.done",
                    "text": "Verified answer",
                }
            )
            + "\n"
        ).encode()
        yield (
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "response-1",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                    },
                }
            )
            + "\n"
        ).encode()

    service = ChatAuditJobService()
    service._create_row = AsyncMock()  # type: ignore[method-assign]
    service._checkpoint = AsyncMock()  # type: ignore[method-assign]
    await service.start(
        audit_id="audit-detached",
        user_id="user-1",
        model="gpt-5.6-sol",
        stream=stream(),
    )

    subscriber = service.subscribe("audit-detached", "user-1")
    created = json.loads((await anext(subscriber)).decode())
    assert created == {"type": "openrag.audit.created", "audit_id": "audit-detached"}
    await subscriber.aclose()

    release.set()
    runtime = service._runtimes["audit-detached"]
    assert runtime.task is not None
    await runtime.task

    status = await service.get("audit-detached", "user-1")
    assert status is not None
    assert status["status"] == "completed"
    assert status["response"] == "Verified answer"
    assert status["response_id"] == "response-1"
    assert status["usage"]["total_tokens"] == 110
    assert status["usage"]["cost_usd"] == pytest.approx(0.0006)


@pytest.mark.asyncio
async def test_failed_audit_event_is_terminal_and_contains_the_durable_error() -> None:
    async def stream():
        raise TimeoutError
        yield b""  # pragma: no cover - makes this an async generator

    service = ChatAuditJobService()
    service._create_row = AsyncMock()  # type: ignore[method-assign]
    service._checkpoint = AsyncMock()  # type: ignore[method-assign]
    await service.start(
        audit_id="audit-timeout",
        user_id="user-1",
        model="gpt-5.6-luna",
        stream=stream(),
    )

    runtime = service._runtimes["audit-timeout"]
    assert runtime.task is not None
    await runtime.task
    failed = json.loads(runtime.events[-1].decode())

    assert failed["type"] == "openrag.audit.failed"
    assert failed["status"] == "failed"
    assert failed["error"] == ("Exhaustive audit timed out before source verification completed")
