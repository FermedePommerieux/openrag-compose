"""Read-only OpenArchiver manifest export tests."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _module():
    path = Path(__file__).parents[2] / "scripts" / "export_openarchiver_metadata_manifest.py"
    spec = importlib.util.spec_from_file_location("manifest_export", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_preserves_attachment_identity_and_explicit_mail_parent(tmp_path: Path):
    database = tmp_path / "connector.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE emails(
          id TEXT, source_id TEXT, storage_path TEXT, sha256 TEXT,
          size_bytes INTEGER, openrag_filename TEXT, status TEXT
        );
        CREATE TABLE attachments(
          id TEXT, filename TEXT, storage_path TEXT, sha256 TEXT,
          size_bytes INTEGER, status TEXT
        );
        CREATE TABLE email_attachments(email_id TEXT, attachment_id TEXT);
        INSERT INTO emails VALUES(
          'mail/1', 'source 1', 'emails/mail-1.eml',
          'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          10, 'mail.eml', 'validated'
        );
        INSERT INTO attachments VALUES(
          'attachment/1', 'invoice.pdf', 'attachments/binary-1',
          'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          20, 'validated'
        );
        INSERT INTO email_attachments VALUES('mail/1', 'attachment/1');
        """
    )
    connection.commit()
    connection.close()

    payload = _module().export_manifest(database)
    entries = {item["entity_id"]: item for item in payload["entries"]}
    attachment = entries["urn:openrag:openarchiver:attachment:attachment%2F1"]

    assert attachment["storage_path"] == "attachments/binary-1"
    assert attachment["parent_entity_ids"] == ["urn:openrag:openarchiver:email:source%201:mail%2F1"]
    assert all("source_url" not in item for item in payload["entries"])


def test_export_opens_database_read_only(tmp_path: Path):
    database = tmp_path / "connector.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE emails(id, source_id, storage_path, sha256, size_bytes,
                            openrag_filename, status);
        CREATE TABLE attachments(id, filename, storage_path, sha256, size_bytes, status);
        CREATE TABLE email_attachments(email_id, attachment_id);
        """
    )
    connection.close()
    before = database.stat().st_mtime_ns

    assert _module().export_manifest(database)["entries"] == []

    assert database.stat().st_mtime_ns == before
