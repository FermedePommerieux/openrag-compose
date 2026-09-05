"""Forward/backward schema compatibility preserves external identity and roles."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_local_auth_migration_roundtrip(tmp_path):
    root = Path(__file__).resolve().parents[2]
    database = tmp_path / "identity.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(action, revision):
        subprocess.run(
            [sys.executable, "-m", "alembic", action, revision],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
        )

    migrate("upgrade", "0007_add_knowledge_delete_anonymous")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (id,oauth_provider,oauth_subject,is_active,created_at,updated_at) "
            "VALUES ('external-stable-id','google','provider-subject',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO user_roles (user_id,role_id,granted_at) "
            "VALUES ('external-stable-id','role-user',CURRENT_TIMESTAMP)"
        )
    migrate("upgrade", "head")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM local_credentials").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM auth_sessions").fetchone() == (0,)
    migrate("downgrade", "0007_add_knowledge_delete_anonymous")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id,oauth_provider,oauth_subject FROM users"
        ).fetchall() == [("external-stable-id", "google", "provider-subject")]
        assert connection.execute("SELECT user_id,role_id FROM user_roles").fetchall() == [
            ("external-stable-id", "role-user")
        ]
    migrate("upgrade", "head")
