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


def test_downgrade_preserves_required_password_and_archive_security(tmp_path):
    root = Path(__file__).resolve().parents[2]
    database = tmp_path / "guarded.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(action, revision):
        return subprocess.run(
            [sys.executable, "-m", "alembic", action, revision],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )

    assert migrate("upgrade", "head").returncode == 0
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO users (id,oauth_provider,oauth_subject,is_active,created_at,updated_at) VALUES ('local-id','local','local-id',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO local_credentials (user_id,login,password_hash,version,must_change_password) VALUES ('local-id','admin','fixture-hash',1,1)"
        )
        db.execute("INSERT INTO user_storage VALUES ('local-id','admin')")
        db.execute("INSERT INTO source_archive_locations VALUES ('fixture-source','local-id')")
    result = migrate("downgrade", "0008_local_auth")
    assert result.returncode != 0 and "Restore archived source locations" in result.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0010_user_storage",
        )
        db.execute("DELETE FROM source_archive_locations")
    result = migrate("downgrade", "0008_local_auth")
    assert result.returncode != 0 and "Replace all temporary passwords" in result.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT must_change_password FROM local_credentials").fetchone() == (1,)
        db.execute("UPDATE local_credentials SET must_change_password=0")
    assert migrate("downgrade", "0008_local_auth").returncode == 0
    assert migrate("upgrade", "head").returncode == 0
