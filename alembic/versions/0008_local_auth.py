"""Local credentials and revocable sessions; existing user identities are unchanged."""

import sqlalchemy as sa

from alembic import op

revision = "0008_local_auth"
down_revision = "0007_add_knowledge_delete_anonymous"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_credentials",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("login", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_local_credentials_login", "local_credentials", ["login"], unique=True)
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=True),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])


def downgrade() -> None:
    # Stop authenticated traffic before downgrade. This removes local credentials
    # and sessions, never users or external identity/role/ownership rows.
    op.drop_table("auth_sessions")
    op.drop_table("local_credentials")
