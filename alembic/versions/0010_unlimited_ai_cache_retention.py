"""retain structured AI cache entries without expiration

Revision ID: 0010_unlimited_ai_cache
Revises: 0009_ai_response_cache
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_unlimited_ai_cache"
down_revision: str | Sequence[str] | None = "0009_ai_response_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable expiry represents durable documentary work. Convert existing
    # rows too, rather than keeping the former accidental 30-day deadline.
    with op.batch_alter_table("ai_response_cache") as batch_op:
        batch_op.alter_column(
            "expires_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )
    op.execute(sa.text("UPDATE ai_response_cache SET expires_at = NULL"))


def downgrade() -> None:
    # A downgrade cannot represent unlimited retention. Mark unlimited rows at
    # their last update time so the old reader safely treats them as expired.
    op.execute(
        sa.text(
            "UPDATE ai_response_cache "
            "SET expires_at = updated_at WHERE expires_at IS NULL"
        )
    )
    with op.batch_alter_table("ai_response_cache") as batch_op:
        batch_op.alter_column(
            "expires_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
