"""durable exhaustive chat audit jobs

Revision ID: 0008_chat_audit_jobs
Revises: 0007_add_knowledge_delete_anonymous
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_chat_audit_jobs"
down_revision: str | Sequence[str] | None = "0007_add_knowledge_delete_anonymous"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_audit_jobs",
        sa.Column("audit_id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("response_id", sa.String(length=128), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_chat_audit_jobs_user_id", "chat_audit_jobs", ["user_id"])
    op.create_index("ix_chat_audit_jobs_status", "chat_audit_jobs", ["status"])
    op.create_index("ix_chat_audit_jobs_user_recent", "chat_audit_jobs", ["user_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_audit_jobs_user_recent", table_name="chat_audit_jobs")
    op.drop_index("ix_chat_audit_jobs_status", table_name="chat_audit_jobs")
    op.drop_index("ix_chat_audit_jobs_user_id", table_name="chat_audit_jobs")
    op.drop_table("chat_audit_jobs")
