"""add durable structured AI response cache

Revision ID: 0009_ai_response_cache
Revises: 0008_chat_audit_jobs
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_ai_response_cache"
down_revision: str | Sequence[str] | None = "0008_chat_audit_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_response_cache",
        sa.Column("cache_key", sa.String(length=64), primary_key=True),
        sa.Column("scope_sha256", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("schema_name", sa.String(length=128), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("usage_payload", sa.JSON(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ai_response_cache_scope_sha256", "ai_response_cache", ["scope_sha256"])
    op.create_index("ix_ai_response_cache_namespace", "ai_response_cache", ["namespace"])
    op.create_index(
        "ix_ai_response_cache_scope_namespace",
        "ai_response_cache",
        ["scope_sha256", "namespace"],
    )
    op.create_index("ix_ai_response_cache_expires_at", "ai_response_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_response_cache_expires_at", table_name="ai_response_cache")
    op.drop_index("ix_ai_response_cache_scope_namespace", table_name="ai_response_cache")
    op.drop_index("ix_ai_response_cache_namespace", table_name="ai_response_cache")
    op.drop_index("ix_ai_response_cache_scope_sha256", table_name="ai_response_cache")
    op.drop_table("ai_response_cache")
