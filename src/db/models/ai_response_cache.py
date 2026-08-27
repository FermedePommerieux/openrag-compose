"""Durable, user-scoped cache for verified structured AI work.

The cache deliberately stores neither prompts nor raw request bodies.  Its key
is a SHA-256 digest over the complete request contract (scope, model, schema,
prompt and cache version), while the validated structured response and the
original usage summary are retained for reuse and cost reporting.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Index
from sqlmodel import Field, SQLModel


class AIResponseCache(SQLModel, table=True):
    __tablename__ = "ai_response_cache"
    __table_args__ = (
        Index("ix_ai_response_cache_scope_namespace", "scope_sha256", "namespace"),
        Index("ix_ai_response_cache_expires_at", "expires_at"),
    )

    cache_key: str = Field(primary_key=True, max_length=64)
    scope_sha256: str = Field(max_length=64, index=True)
    namespace: str = Field(max_length=64, index=True)
    model: str = Field(max_length=128)
    schema_name: str = Field(max_length=128)
    response_payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    usage_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    hit_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
