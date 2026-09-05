"""Require replacement of operator-issued temporary local passwords."""

import sqlalchemy as sa

from alembic import op

revision = "0009_password_change"
down_revision = "0008_local_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "local_credentials",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    # Refuse to turn a temporary password into an unrestricted credential.
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM local_credentials WHERE must_change_password = true LIMIT 1")
    ).first():
        raise RuntimeError("Replace all temporary passwords before downgrading")
    with op.batch_alter_table("local_credentials") as batch:
        batch.drop_column("must_change_password")
