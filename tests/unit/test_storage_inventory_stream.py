"""Bounded inventory preserves ambiguous ownership and rejects partial scrolls."""

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "storage_inventory_stream",
    Path(__file__).parents[2] / "scripts/inventory_local_storage_stream.py",
)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def test_file_owner_assignment_is_total_and_foreign_acl_is_unknown(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "unindexed.txt").write_bytes(b"pending")
    (root / "owned.txt").write_bytes(b"existing")
    db = tmp_path / "users.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE users(id TEXT, oauth_provider TEXT)")
    import base64
    import hashlib

    docid = base64.urlsafe_b64encode(hashlib.sha256(b"existing").digest()).decode()[:24]
    hit = {
        "_index": "documents",
        "_id": "c",
        "_seq_no": 1,
        "_primary_term": 1,
        "_source": {"document_id": docid, "owner": "foreign"},
    }
    search = SimpleNamespace(scan=lambda *a, **k: iter([hit]), request=lambda *a, **k: {})
    monkeypatch.setattr(inventory, "Search", lambda: search)
    monkeypatch.setattr(
        "sys.argv",
        [
            "inventory",
            "--target-user-id",
            "planned-user",
            "--documents",
            str(root),
            "--archive",
            str(root / "archive"),
            "--database",
            str(db),
        ],
    )
    rows = []
    monkeypatch.setattr(
        inventory, "emit", lambda event, **fields: rows.append({"event": event, **fields})
    )
    inventory.main()
    owners = [r for r in rows if r["event"] == "file_owner"]
    assert len(owners) == 2
    assert (
        next(r for r in owners if r["source"].endswith("unindexed.txt"))["assignment"] == "ASSIGNED"
    )
    assert next(r for r in owners if r["source"].endswith("owned.txt"))["reasons"] == [
        "existing_other_owner"
    ]
    assert rows[-1]["file_assignments"] == {"UNKNOWN": 1, "ASSIGNED": 1}
    assert not (root / "eloiprimaux").exists()


@pytest.mark.parametrize("partial", [False, True])
def test_stream_exact_total_and_scroll_cleanup(partial):
    search = inventory.Search.__new__(inventory.Search)
    pages = iter(
        [
            {
                "_scroll_id": "cursor",
                "timed_out": False,
                "_shards": {"total": 1, "successful": 1, "failed": 0},
                "hits": {
                    "total": {"value": 2 if partial else 1, "relation": "eq"},
                    "hits": [{"_id": "one"}],
                },
            },
            {
                "_scroll_id": "cursor",
                "timed_out": False,
                "_shards": {"total": 1, "successful": 1, "failed": 0},
                "hits": {"total": {"value": 2 if partial else 1, "relation": "eq"}, "hits": []},
            },
        ]
    )
    cleaned = []

    def request(path, body=None, method=None):
        if method == "DELETE":
            cleaned.append(body)
            return {}
        return next(pages)

    search.request = request
    if partial:
        with pytest.raises(ValueError, match="before its exact total"):
            list(search.scan("documents", [], {}))
    else:
        assert list(search.scan("documents", [], {})) == [{"_id": "one"}]
    assert cleaned == [{"scroll_id": ["cursor"]}]


def test_jsonl_output_keeps_event_and_file_kind_separate(capsys):
    import json

    inventory.emit("file", kind="ingestion")
    assert json.loads(capsys.readouterr().out) == {"event": "file", "kind": "ingestion"}
