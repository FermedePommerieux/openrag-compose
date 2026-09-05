"""Attach per-user filesystem roots and stable source locators to application users."""

import sqlalchemy as sa

from alembic import op

revision = "0010_user_storage"
down_revision = "0009_password_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_storage",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("directory", sa.String(64), nullable=False, unique=True),
    )
    op.create_table(
        "source_archive_locations",
        sa.Column("source_id", sa.String(161), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_storage.user_id"), nullable=False),
    )
    op.create_index("ix_source_archive_locations_user_id", "source_archive_locations", ["user_id"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM source_archive_locations LIMIT 1")).first():
        raise RuntimeError("Restore archived source locations before downgrading")
    op.drop_table("source_archive_locations")
    op.drop_table("user_storage")
