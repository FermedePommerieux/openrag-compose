"""Migration preparation is read-only, exact, and excludes foreign ownership."""

import base64
import hashlib
import sqlite3
from types import SimpleNamespace

from scripts.plan_local_storage_migration import build_plan, file_inventory, propose_acl


def test_inventory_hashes_destinations_and_rejects_symlinks(tmp_path):
    documents = tmp_path / "documents"
    archive = documents / ".openrag-indexed"
    archive.mkdir(parents=True)
    digest = hashlib.sha256(b"archived bytes").digest()
    docid = base64.urlsafe_b64encode(digest).decode()[:24]
    sourceid = docid + "." + "a" * 32
    (archive / sourceid).mkdir()
    (archive / sourceid / "original.txt").write_bytes(b"archived bytes")
    (documents / "pending.txt").write_bytes(b"pending")
    (tmp_path / "outside").mkdir()
    (documents / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    entries, blockers = file_inventory(documents, archive, "eloiprimaux")
    assert len(entries) == 2 and blockers[0]["reason"] == "symlink"
    assert {item["destination"] for item in entries} == {
        str(documents / "eloiprimaux/ingestion/pending.txt"),
        str(documents / "eloiprimaux/archives" / sourceid / "original.txt"),
    }
    assert (archive / sourceid / "original.txt").read_bytes() == b"archived bytes"
    assert not (documents / "eloiprimaux").exists()


def test_acl_plan_preserves_foreign_users_and_refuses_implicit_sharing_changes():
    hit = {
        "_index": "documents",
        "_id": "chunk",
        "_seq_no": 7,
        "_primary_term": 2,
        "_source": {"document_id": "doc"},
    }
    change, reason = propose_acl(hit, "planned-id", "eloiprimaux")
    assert reason is None and change["if_seq_no"] == 7
    assert "owner" in change["before_absent"] and change["after"]["owner"] == "planned-id"
    hit["_source"]["owner"] = "external-reader"
    assert propose_acl(hit, "planned-id", "eloiprimaux") == (None, "different_owner")
    hit["_source"]["owner"] = None
    hit["_source"]["allowed_users"] = ["existing-shared-reader"]
    assert propose_acl(hit, "planned-id", "eloiprimaux") == (
        None,
        "explicit_sharing_requires_review",
    )


def test_plan_reserves_admin_uuid_without_creating_account_or_files(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE users (id text primary key)")
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "draft.txt").write_text("draft")
    args = SimpleNamespace(
        documents=str(documents),
        archive=str(documents / ".openrag-indexed"),
        database=str(database),
        login="eloiprimaux",
        user_id="a95b2273-9b82-49d1-aaef-13dc4eac931c",
        index="documents",
    )
    plan = build_plan(args)
    assert plan["execution_state"] == "PLANNED_ONLY"
    assert plan["target"]["user_id"] == args.user_id and plan["target"]["require_password_change"]
    assert plan["target"]["create_account"] and plan["target"]["role"] == "admin"
    assert plan["blockers"] == [{"reason": "indexed_ownership_and_metadata_not_inventoried"}]
    assert not (documents / "eloiprimaux").exists()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_large_index_is_counted_without_loading_chunks(tmp_path):
    from unittest.mock import Mock

    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE users (id text primary key)")
    documents = tmp_path / "documents"
    documents.mkdir()
    search = Mock()
    search.count.return_value = 652000
    search.request.return_value = {"metadata-generation": {}}
    plan = build_plan(
        SimpleNamespace(
            documents=str(documents),
            archive=str(documents / ".openrag-indexed"),
            database=str(database),
            login="eloiprimaux",
            user_id=None,
            index="documents",
        ),
        search,
    )
    assert plan["matching_chunk_count"] == 652000
    assert plan["index_inventory_complete"] is False
    assert plan["blockers"][0]["reason"] == "chunk_scope_exceeds_bounded_planner"
    search.scan.assert_not_called()


def test_http_inventory_rejects_oversize_responses_and_writes(monkeypatch):
    from unittest.mock import MagicMock

    import pytest

    from scripts import plan_local_storage_migration as planner

    monkeypatch.setenv("OPENSEARCH_PASSWORD", "fixture-only")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"x" * (2 * 1024 * 1024 + 1)
    monkeypatch.setattr(planner, "urlopen", lambda *a, **k: response)
    search = planner.ReadOnlySearch()
    with pytest.raises(ValueError, match="2 MiB"):
        search.request("/documents/_search", {"size": 0})
    response.__enter__.return_value.read.assert_called_once_with(2 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="refuses"):
        search.request("/documents/_update_by_query", {})


def test_foreign_file_owner_blocks_physical_migration(tmp_path):
    from unittest.mock import Mock

    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE users (id text primary key)")
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "owned.txt").write_bytes(b"owned")
    docid = base64.urlsafe_b64encode(hashlib.sha256(b"owned").digest()).decode()[:24]
    search = Mock()
    search.count.return_value = 1
    search.request.return_value = {}
    search.scan.return_value = [
        {
            "_index": "documents",
            "_id": "foreign-chunk",
            "_source": {"document_id": docid, "owner": "other-user"},
        }
    ]
    plan = build_plan(
        SimpleNamespace(
            documents=str(documents),
            archive=str(documents / ".openrag-indexed"),
            database=str(database),
            login="eloiprimaux",
            user_id=None,
            index="documents",
        ),
        search,
    )
    assert plan["chunk_acl_changes"] == []
    assert plan["blockers"] == [
        {"reason": "physical_files_have_foreign_ownership_or_sharing", "document_ids": [docid]}
    ]
