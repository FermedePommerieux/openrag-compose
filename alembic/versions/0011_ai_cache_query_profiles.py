"""add conservative semantic query profiles to the AI cache

Revision ID: 0011_ai_cache_query_profiles
Revises: 0010_unlimited_ai_cache
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_ai_cache_query_profiles"
down_revision: str | Sequence[str] | None = "0010_unlimited_ai_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_response_cache",
        sa.Column("semantic_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_response_cache",
        sa.Column("query_profile", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_ai_response_cache_semantic_key",
        "ai_response_cache",
        ["semantic_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_response_cache_semantic_key", table_name="ai_response_cache")
    op.drop_column("ai_response_cache", "query_profile")
    op.drop_column("ai_response_cache", "semantic_key")
