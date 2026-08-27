"""Durable execution record for long exhaustive chat audits.

The HTTP/SSE connection is only a viewer.  The audit producer owns the
Langflow stream and writes its terminal answer, progress certificate and
metered model usage here so a browser can reconnect without restarting work.
Raw prompts, JWTs, provider keys and document text are deliberately excluded.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


class ChatAuditJob(SQLModel, table=True):
    __tablename__ = "chat_audit_jobs"
    __table_args__ = (Index("ix_chat_audit_jobs_user_recent", "user_id", "updated_at"),)

    audit_id: str = Field(primary_key=True, max_length=64)
    user_id: str = Field(max_length=64, index=True)
    status: str = Field(default="running", max_length=32, index=True)
    response_id: str | None = Field(default=None, max_length=128)
    response_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    progress: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    error: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = Field(default=None)
