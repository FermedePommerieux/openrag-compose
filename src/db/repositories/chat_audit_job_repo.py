"""Async persistence for resumable exhaustive chat audits."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from db.models.chat_audit_job import ChatAuditJob


class ChatAuditJobRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, audit_id: str) -> ChatAuditJob | None:
        return await self.session.get(ChatAuditJob, audit_id)

    async def create(self, *, audit_id: str, user_id: str) -> ChatAuditJob:
        row = ChatAuditJob(audit_id=audit_id, user_id=user_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(
        self,
        audit_id: str,
        *,
        status: str | None = None,
        response_id: str | None = None,
        response_text: str | None = None,
        progress: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        terminal: bool = False,
    ) -> ChatAuditJob | None:
        row = await self.get(audit_id)
        if row is None:
            return None
        if status is not None:
            row.status = status
        if response_id is not None:
            row.response_id = response_id
        if response_text is not None:
            row.response_text = response_text
        if progress is not None:
            row.progress = progress
        if usage is not None:
            row.usage = usage
        if error is not None:
            row.error = error[:2_000]
        now = datetime.now(UTC)
        row.updated_at = now
        if terminal:
            row.completed_at = now
        self.session.add(row)
        await self.session.flush()
        return row
