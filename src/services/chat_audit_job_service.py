"""Detached, durable execution for exhaustive chat audits.

The producer task consumes Langflow independently of any browser subscriber.
Disconnecting an SSE response therefore stops only that subscriber. Progress,
the final answer and complete model usage are checkpointed in OpenRAG's
persistent backend database and can be polled by the owning user.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from db import engine as db_engine
from db.repositories.chat_audit_job_repo import ChatAuditJobRepo
from services.audit_progress_service import audit_progress_service
from services.token_usage_service import token_usage_service
from utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class _Runtime:
    audit_id: str
    user_id: str
    model: str
    events: list[bytes] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: bool = False
    response_text: str = ""
    response_id: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    task: asyncio.Task[None] | None = None


def _event_data(chunk: bytes | str) -> dict[str, Any] | None:
    raw = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
    try:
        value = json.loads(raw.strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _delta_text(event: dict[str, Any]) -> str:
    # Responses emits ``response.output_text.done`` with the complete text
    # after the incremental events. Appending both would duplicate every
    # recovered answer, so only delta payloads belong in this buffer.
    event_type = str(event.get("type") or "")
    if event_type and not event_type.endswith(".delta"):
        return ""
    delta = event.get("delta")
    if isinstance(delta, dict):
        return str(delta.get("content") or delta.get("text") or "")
    if isinstance(delta, str):
        return delta
    return ""


class ChatAuditJobService:
    """Own exhaustive execution lifetime and expose reconnect-safe status."""

    def __init__(self) -> None:
        self._runtimes: dict[str, _Runtime] = {}
        self._lock = asyncio.Lock()

    async def _session(self):
        if db_engine.SessionLocal is None:
            db_engine.init_engine()
        assert db_engine.SessionLocal is not None
        return db_engine.SessionLocal()

    async def _create_row(self, audit_id: str, user_id: str) -> None:
        async with await self._session() as session:
            repo = ChatAuditJobRepo(session)
            await repo.create(audit_id=audit_id, user_id=user_id)
            await session.commit()

    async def _checkpoint(self, runtime: _Runtime, *, status: str, terminal: bool) -> None:
        async with await self._session() as session:
            repo = ChatAuditJobRepo(session)
            await repo.update(
                runtime.audit_id,
                status=status,
                response_id=runtime.response_id,
                response_text=runtime.response_text,
                progress=runtime.progress,
                usage=runtime.usage,
                error=runtime.error,
                terminal=terminal,
            )
            await session.commit()

    async def start(
        self,
        *,
        audit_id: str,
        user_id: str,
        model: str,
        stream: AsyncIterator[bytes],
    ) -> None:
        token_usage_service.reset(audit_id)
        await self._create_row(audit_id, user_id)
        runtime = _Runtime(audit_id=audit_id, user_id=user_id, model=model)
        async with self._lock:
            self._runtimes[audit_id] = runtime
        # This task, rather than the StreamingResponse generator, owns Langflow.
        # Client cancellation can no longer propagate into the audit producer.
        runtime.task = asyncio.create_task(
            self._produce(runtime, stream), name=f"chat-audit-{audit_id}"
        )

    async def _publish(self, runtime: _Runtime, chunk: bytes) -> None:
        async with runtime.condition:
            runtime.events.append(chunk)
            runtime.condition.notify_all()

    async def _produce(self, runtime: _Runtime, stream: AsyncIterator[bytes]) -> None:
        status = "completed"
        try:
            async for chunk in stream:
                await self._publish(runtime, chunk)
                event = _event_data(chunk)
                if event is None:
                    continue
                if event.get("type") == "openrag.audit.progress":
                    progress = event.get("progress")
                    if isinstance(progress, dict):
                        runtime.progress = progress
                        runtime.usage = token_usage_service.snapshot(runtime.audit_id)
                        await self._checkpoint(runtime, status="running", terminal=False)
                    continue
                runtime.response_text += _delta_text(event)
                response = event.get("response") if isinstance(event.get("response"), dict) else {}
                runtime.response_id = (
                    event.get("id")
                    or event.get("response_id")
                    or response.get("id")
                    or runtime.response_id
                )
                if event.get("type") == "response.completed" and response.get("usage"):
                    token_usage_service.record_usage(
                        runtime.model, response["usage"], audit_id=runtime.audit_id
                    )
            runtime.progress = audit_progress_service.snapshot(runtime.audit_id) or runtime.progress
            runtime.usage = token_usage_service.snapshot(runtime.audit_id)
            if not runtime.response_text.strip():
                status = "failed"
                runtime.error = "Audit completed without a recoverable assistant response"
                audit_progress_service.fail(runtime.audit_id)
                runtime.progress = (
                    audit_progress_service.snapshot(runtime.audit_id) or runtime.progress
                )
            await self._publish(
                runtime,
                (
                    json.dumps(
                        {
                            "type": "openrag.audit.usage",
                            "audit_id": runtime.audit_id,
                            "usage": runtime.usage,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode(),
            )
        except Exception as error:
            status = "failed"
            runtime.error = str(error)
            audit_progress_service.fail(runtime.audit_id)
            runtime.progress = audit_progress_service.snapshot(runtime.audit_id) or runtime.progress
            runtime.usage = token_usage_service.snapshot(runtime.audit_id)
            logger.exception("Detached exhaustive chat audit failed", audit_id=runtime.audit_id)
            await self._publish(
                runtime,
                (
                    json.dumps(
                        {"type": "openrag.audit.failed", "audit_id": runtime.audit_id},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode(),
            )
        finally:
            runtime.done = True
            await self._checkpoint(runtime, status=status, terminal=True)
            async with runtime.condition:
                runtime.condition.notify_all()

    async def subscribe(self, audit_id: str, user_id: str) -> AsyncIterator[bytes]:
        runtime = self._runtimes.get(audit_id)
        if runtime is None or runtime.user_id != user_id:
            return
        yield (
            json.dumps({"type": "openrag.audit.created", "audit_id": audit_id}, ensure_ascii=False)
            + "\n"
        ).encode()
        cursor = 0
        while True:
            async with runtime.condition:
                while cursor >= len(runtime.events) and not runtime.done:
                    await runtime.condition.wait()
                pending = runtime.events[cursor:]
                cursor = len(runtime.events)
                done = runtime.done
            for chunk in pending:
                yield chunk
            if done and cursor >= len(runtime.events):
                break

    async def get(self, audit_id: str, user_id: str) -> dict[str, Any] | None:
        runtime = self._runtimes.get(audit_id)
        if runtime is not None and runtime.user_id == user_id:
            return {
                "audit_id": audit_id,
                "status": "failed" if runtime.error else "completed" if runtime.done else "running",
                "response_id": runtime.response_id,
                "response": runtime.response_text if runtime.done else None,
                "progress": runtime.progress or audit_progress_service.snapshot(audit_id) or {},
                "usage": runtime.usage or token_usage_service.snapshot(audit_id),
                "error": runtime.error,
            }
        async with await self._session() as session:
            row = await ChatAuditJobRepo(session).get(audit_id)
            if row is None or row.user_id != user_id:
                return None
            return {
                "audit_id": row.audit_id,
                "status": row.status,
                "response_id": row.response_id,
                "response": row.response_text if row.status != "running" else None,
                "progress": row.progress,
                "usage": row.usage,
                "error": row.error,
                "created_at": row.created_at.replace(tzinfo=UTC).isoformat(),
                "updated_at": row.updated_at.replace(tzinfo=UTC).isoformat(),
                "completed_at": (
                    row.completed_at.replace(tzinfo=UTC).isoformat() if row.completed_at else None
                ),
            }


chat_audit_job_service = ChatAuditJobService()
