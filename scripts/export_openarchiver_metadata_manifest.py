#!/usr/bin/env python3
"""Export a read-only, fail-closed backfill manifest from connector SQLite."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any


def _entity_id(kind: str, object_id: str, *, source_id: str = "") -> str:
    parts = ["urn", "openrag", "openarchiver", kind]
    if source_id:
        parts.append(urllib.parse.quote(source_id, safe=""))
    parts.append(urllib.parse.quote(object_id, safe=""))
    return ":".join(parts)


def export_manifest(database_path: Path) -> dict[str, Any]:
    """Read the connector registry without creating journals or mutating rows."""
    uri = f"file:{urllib.parse.quote(str(database_path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        # The production connector has a read-only root filesystem.  Keep
        # ORDER BY scratch space in memory and make the read-only intent
        # explicit so an export never needs writable SQLite temp storage.
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        email_rows = connection.execute(
            """
            SELECT id, source_id, storage_path, sha256, size_bytes,
                   openrag_filename, status
            FROM emails
            ORDER BY source_id, id
            """
        ).fetchall()
        attachment_rows = connection.execute(
            """
            SELECT id, filename, storage_path, sha256, size_bytes, status
            FROM attachments
            ORDER BY id
            """
        ).fetchall()
        parent_rows = connection.execute(
            """
            SELECT ea.attachment_id, e.id AS email_id, e.source_id
            FROM email_attachments ea
            JOIN emails e ON e.id=ea.email_id
            ORDER BY ea.attachment_id, e.source_id, e.id
            """
        ).fetchall()
    finally:
        connection.close()

    parents: dict[str, list[str]] = defaultdict(list)
    for row in parent_rows:
        parents[str(row["attachment_id"])].append(
            _entity_id("email", str(row["email_id"]), source_id=str(row["source_id"]))
        )

    entries: list[dict[str, Any]] = []
    for row in email_rows:
        entries.append(
            {
                "entity_id": _entity_id("email", str(row["id"]), source_id=str(row["source_id"])),
                "archive_source": "openarchiver",
                "archive_object_id": str(row["id"]),
                "original_name": str(row["openrag_filename"]),
                "storage_path": str(row["storage_path"]),
                "expected_sha256": str(row["sha256"]),
                "size_bytes": int(row["size_bytes"]),
                "status": str(row["status"]),
                "parent_entity_ids": [],
            }
        )
    for row in attachment_rows:
        attachment_id = str(row["id"])
        entries.append(
            {
                "entity_id": _entity_id("attachment", attachment_id),
                "archive_source": "openarchiver",
                "archive_object_id": attachment_id,
                "original_name": str(row["filename"]),
                "storage_path": str(row["storage_path"]),
                "expected_sha256": str(row["sha256"]),
                "size_bytes": int(row["size_bytes"]),
                "status": str(row["status"]),
                "parent_entity_ids": list(dict.fromkeys(parents[attachment_id])),
            }
        )
    entries.sort(key=lambda item: item["entity_id"])
    return {
        "schema_version": 1,
        "source": "openarchiver_connector_sqlite_read_only",
        "entries": entries,
    }


def _atomic_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export an ephemeral OpenArchiver metadata-backfill manifest"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    _atomic_private_json(arguments.output, export_manifest(arguments.database))
