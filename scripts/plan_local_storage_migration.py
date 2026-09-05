#!/usr/bin/env python3
"""Read-only plan for retiring legacy shared storage into one local account.

No account, file, document, alias, or auth policy is changed. Outputs an exact
filesystem inventory, proposed ACL changes, and the coordinated rollback steps.
Use a separate maintenance process with read-only volume access and the backend's
configuration. Do not run a corpus inventory inside the serving backend pod.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import ssl
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE = re.compile(r"^(?P<document>[A-Za-z0-9_-]{16,128})\.[a-f0-9]{32}$")
LOGIN = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}")
ACL_FIELDS = (
    "owner",
    "owner_name",
    "owner_email",
    "allowed_users",
    "allowed_groups",
    "allowed_principals",
)
PROJECTION_ALIAS = "documents-metadata-filter-current"


def canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_inventory(documents: Path, archive: Path, directory: str, managed_directories=()):
    entries = []
    blockers = []
    excluded = set(managed_directories) | {directory}
    roots = [("ingestion", documents), ("archives", archive)]
    for kind, root in roots:
        if not root.exists():
            continue
        if root.is_symlink():
            blockers.append({"reason": "symlink_root", "path": str(root)})
            continue
        for current_name, dirs, files in os.walk(root, followlinks=False):
            current = Path(current_name)
            retained_dirs = []
            for name in sorted(dirs):
                path = current / name
                if kind == "ingestion" and (
                    path == archive or (current == documents and name in excluded)
                ):
                    continue
                if path.is_symlink():
                    blockers.append({"reason": "symlink", "path": str(path)})
                else:
                    retained_dirs.append(name)
            dirs[:] = retained_dirs
            for name in sorted(files):
                path = current / name
                if path.is_symlink() or not path.is_file():
                    blockers.append({"reason": "non_regular_file", "path": str(path)})
                    continue
                relative = path.relative_to(root)
                source_id = relative.parts[0] if kind == "archives" else None
                if source_id is not None and (
                    len(relative.parts) != 2 or not SOURCE.fullmatch(source_id)
                ):
                    blockers.append({"reason": "unrecognized_archive_layout", "path": str(path)})
                    continue
                before = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ):
                    blockers.append({"reason": "file_changed_during_inventory", "path": str(path)})
                    continue
                document_id = base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")[:24]
                if (
                    source_id is not None
                    and SOURCE.fullmatch(source_id).group("document") != document_id
                ):
                    blockers.append({"reason": "archive_content_id_mismatch", "path": str(path)})
                    continue
                entries.append(
                    {
                        "kind": kind,
                        "source": str(path),
                        "destination": str(documents / directory / kind / relative),
                        "bytes": after.st_size,
                        "sha256": digest.hexdigest(),
                        "mtime_ns": after.st_mtime_ns,
                        "mode": oct(after.st_mode & 0o777),
                        "uid": after.st_uid,
                        "gid": after.st_gid,
                        "source_id": source_id,
                        "document_id": document_id,
                    }
                )
    return sorted(entries, key=lambda x: str(x["source"])), blockers


def propose_acl(hit, target_id, target_login):
    source = hit.get("_source", {})
    if source.get("owner") not in (None, "anonymous"):
        return None, "different_owner"
    if source.get("connector_type") not in (None, "local"):
        return None, "different_connector"
    for field in ("allowed_groups", "allowed_principals"):
        if source.get(field):
            return None, "explicit_sharing_requires_review"
    if any(value != "anonymous" for value in (source.get("allowed_users") or [])):
        return None, "explicit_sharing_requires_review"
    before = {key: source[key] for key in ACL_FIELDS if key in source}
    after = {
        "owner": target_id,
        "owner_name": target_login,
        "owner_email": None,
        "allowed_users": [],
        "allowed_groups": [],
        "allowed_principals": [],
    }
    return {
        "index": hit["_index"],
        "id": hit["_id"],
        "if_seq_no": hit["_seq_no"],
        "if_primary_term": hit["_primary_term"],
        "document_id": source.get("document_id"),
        "before": before,
        "before_absent": [k for k in ACL_FIELDS if k not in source],
        "after": after,
    }, None


class ReadOnlySearch:
    def __init__(self):
        host = os.environ.get("OPENSEARCH_HOST", "localhost")
        port = os.environ.get("OPENSEARCH_PORT", "9200")
        self.base = f"https://{host}:{port}"
        token = base64.b64encode(
            (
                os.getenv("OPENSEARCH_USERNAME", "admin") + ":" + os.environ["OPENSEARCH_PASSWORD"]
            ).encode()
        ).decode()
        self.authorization = "Basic " + token

    def request(self, path, body=None):
        # Only search, scroll lifecycle, alias discovery and count reads exist.
        allowed = (
            path.endswith("/_search?scroll=2m")
            or path.endswith("/_search")
            or path in ("/_search/scroll", "/_search/scroll?clear=true")
            or path.startswith("/_alias/")
        )
        if not allowed:
            raise ValueError("Read-only planner refuses this endpoint")
        method = "GET" if body is None else "POST"
        if path.endswith("?clear=true"):
            path = "/_search/scroll"
            method = "DELETE"
        request = Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.authorization, "Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, context=ssl._create_unverified_context(), timeout=30) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError(
                    "Inventory response exceeds the 2 MiB safety bound; run a narrower inventory outside the serving pod"
                )
            return json.loads(payload)

    def scan(self, index, query, fields):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", index):
            raise ValueError("An exact index or alias name is required")
        body = {
            "query": query,
            "size": 25,
            "sort": ["_doc"],
            "_source": fields,
            "seq_no_primary_term": True,
        }
        scroll_id = None
        total = 0
        try:
            page = self.request("/" + index + "/_search?scroll=2m", body)
            while True:
                scroll_id = page.get("_scroll_id", scroll_id)
                hits = page.get("hits", {}).get("hits", [])
                if not hits:
                    break
                total += len(hits)
                if total > 50000:
                    raise ValueError(
                        "Inventory exceeds 50,000 rows; split the scope outside the serving pod"
                    )
                yield from hits
                if not scroll_id:
                    break
                page = self.request("/_search/scroll", {"scroll": "2m", "scroll_id": scroll_id})
        finally:
            if scroll_id:
                self.request("/_search/scroll?clear=true", {"scroll_id": [scroll_id]})

    def count(self, index, query):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", index):
            raise ValueError("An exact index or alias name is required")
        result = self.request(
            "/" + index + "/_search", {"query": query, "size": 0, "track_total_hits": True}
        )
        return result["hits"]["total"]["value"]


def build_plan(args, search=None):
    documents = Path(args.documents).expanduser().absolute()
    archive = Path(args.archive).expanduser().absolute()
    if documents.is_symlink() or archive.is_symlink():
        raise ValueError("Migration roots cannot be symlinks")
    documents, archive = documents.resolve(), archive.resolve()
    login = args.login.strip().lower()
    if not LOGIN.fullmatch(login):
        raise ValueError("Invalid local login")
    database = Path(args.database).expanduser().resolve()
    account = None
    managed = []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "local_credentials" in tables:
            account = db.execute(
                "SELECT u.id,u.is_active FROM users u JOIN local_credentials c ON c.user_id=u.id WHERE c.login=?",
                (login,),
            ).fetchone()
        if "user_storage" in tables:
            managed = [row[0] for row in db.execute("SELECT directory FROM user_storage")]
        if account:
            roles = [
                row[0]
                for row in db.execute(
                    "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?",
                    (account[0],),
                )
            ]
            if not account[1] or "admin" not in roles:
                raise ValueError("Existing target must be an active administrator")
            directory_row = (
                db.execute(
                    "SELECT directory FROM user_storage WHERE user_id=?", (account[0],)
                ).fetchone()
                if "user_storage" in tables
                else None
            )
            directory = directory_row[0] if directory_row else login
        else:
            directory = login
    if not LOGIN.fullmatch(directory) or (documents / directory).is_symlink():
        raise ValueError("Invalid or symlinked destination namespace")
    target_id = account[0] if account else (args.user_id or str(uuid.uuid4()))
    if uuid.UUID(target_id).version != 4:
        raise ValueError("Target identity must be a random UUID v4")
    entries, blockers = file_inventory(documents, archive, directory, managed)
    if (documents / directory).exists() and directory not in managed:
        blockers.append(
            {
                "reason": "unmanaged_destination_namespace_exists",
                "path": str(documents / directory),
            }
        )
    for item in entries:
        if Path(item["destination"]).exists():
            blockers.append({"reason": "destination_exists", "path": item["destination"]})
    document_ids = sorted({item["document_id"] for item in entries})
    changes = []
    excluded = []
    side_documents = []
    alias_targets = []
    matching_chunk_count = None
    index_inventory_complete = False
    if search is not None:
        query = {
            "bool": {
                "should": [
                    {"terms": {"document_id": document_ids}},
                    {
                        "bool": {
                            "filter": [{"term": {"connector_type": "local"}}],
                            "must_not": [{"exists": {"field": "owner"}}],
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }
        fields = list(ACL_FIELDS) + ["document_id", "source_url", "connector_type"]
        matching_chunk_count = search.count(args.index, query)
        index_inventory_complete = matching_chunk_count <= 50000
        if not index_inventory_complete:
            blockers.append(
                {
                    "reason": "chunk_scope_exceeds_bounded_planner",
                    "count": matching_chunk_count,
                    "limit": 50000,
                    "next_step": "Capture ACL journals in bounded batches from a separate maintenance process before cutover",
                }
            )
        for hit in search.scan(args.index, query, fields) if index_inventory_complete else ():
            change, reason = propose_acl(hit, target_id, login)
            if change:
                changes.append(change)
            else:
                excluded.append(
                    {
                        "index": hit["_index"],
                        "id": hit["_id"],
                        "document_id": hit["_source"].get("document_id"),
                        "reason": reason,
                    }
                )
        changed_ids = {item["document_id"] for item in changes}
        ambiguous = changed_ids & {item["document_id"] for item in excluded}
        if ambiguous:
            blockers.append(
                {
                    "reason": "mixed_ownership_or_sharing_for_document_ids",
                    "document_ids": sorted(ambiguous),
                }
            )
        foreign_files = set(document_ids) & {item["document_id"] for item in excluded}
        if foreign_files:
            blockers.append(
                {
                    "reason": "physical_files_have_foreign_ownership_or_sharing",
                    "document_ids": sorted(foreign_files),
                }
            )
        try:
            alias_targets = sorted(search.request("/_alias/" + PROJECTION_ALIAS))
        except Exception as error:
            if getattr(error, "code", None) != 404:
                raise
        if len(alias_targets) > 1:
            blockers.append(
                {"reason": "metadata_alias_has_multiple_targets", "targets": alias_targets}
            )
        if alias_targets and changed_ids:
            projection_fields = [
                "projection_document_id",
                "source_document_id",
                "source_entity_id",
                "representative_chunk_id",
                "owner",
                "allowed_users",
                "allowed_groups",
                "allowed_principals",
                "filter.contract",
                "filter.projection_sha256",
                "filter.source_context_sha256",
                "filter.source_metadata_facts_sha256",
            ]
            for hit in search.scan(
                PROJECTION_ALIAS,
                {"terms": {"source_document_id": sorted(changed_ids)}},
                projection_fields,
            ):
                original = hit["_source"]
                after = dict(original)
                if original.get("owner") not in (None, "anonymous"):
                    blockers.append(
                        {"reason": "metadata_foreign_owner_requires_review", "id": hit["_id"]}
                    )
                if original.get("source_document_id") in changed_ids and original.get("owner") in (
                    None,
                    "anonymous",
                ):
                    if any(
                        original.get(k)
                        for k in ("allowed_users", "allowed_groups", "allowed_principals")
                    ):
                        blockers.append(
                            {"reason": "metadata_sharing_requires_review", "id": hit["_id"]}
                        )
                    after.update(
                        owner=target_id, allowed_users=[], allowed_groups=[], allowed_principals=[]
                    )
                    after["projection_document_id"] = canonical_hash(
                        {
                            "contract": after["filter"]["contract"],
                            "owner": target_id,
                            "source_document_id": after["source_document_id"],
                            "source_entity_id": after["source_entity_id"],
                        }
                    )
                side_documents.append(
                    {
                        "before_id": hit["_id"],
                        "if_seq_no": hit["_seq_no"],
                        "if_primary_term": hit["_primary_term"],
                        "before_acl_and_identity": original,
                        "after_acl_and_identity": after,
                        "filter_payload": "copy unchanged from the original immutable generation; validate projection_sha256",
                    }
                )
    else:
        blockers.append({"reason": "indexed_ownership_and_metadata_not_inventoried"})
    return {
        "schema": "openrag.local-storage-migration-plan.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "execution_state": "PLANNED_ONLY",
        "production_auth_changed": False,
        "target": {
            "login": login,
            "user_id": target_id,
            "role": "admin",
            "create_account": not bool(account),
            "directory": directory,
            "require_password_change": True,
            "temporary_password_source": "operator prompt; never stored in this plan",
        },
        "roots": {
            "legacy_ingestion": str(documents),
            "legacy_archive": str(archive),
            "target_ingestion": str(documents / directory / "ingestion"),
            "target_archive": str(documents / directory / "archives"),
        },
        "files": entries,
        "file_inventory_digest": canonical_hash(entries),
        "chunk_acl_changes": changes,
        "matching_chunk_count": matching_chunk_count,
        "index_inventory_complete": index_inventory_complete,
        "chunk_scope_digest": canonical_hash(changes),
        "excluded_chunks": excluded,
        "metadata": {
            "alias": PROJECTION_ALIAS,
            "original_targets": alias_targets,
            "strategy": "clone immutable generation; rebuild affected projection IDs; verify; atomically switch alias",
            "documents": side_documents,
            "snapshot_digest": canonical_hash(side_documents),
        },
        "source_archive_locations": [
            {"source_id": sid, "user_id": target_id}
            for sid in sorted({x["source_id"] for x in entries if x["source_id"]})
        ],
        "blockers": blockers,
        "cutover": [
            "Resolve all blockers and original production multi-user gates.",
            "Pause all ingestion, reindexing and writes; take a consistent application-DB backup and OpenSearch snapshot.",
            "Bootstrap the planned UUID as a local administrator with --require-password-change; type the approved temporary password at the prompt.",
            "Re-capture and compare every file checksum and chunk seq_no/primary_term; abort on drift.",
            "Copy files without overwriting, verify SHA-256, then register immutable source locators in the same application DB.",
            "Apply only the reviewed ACL deltas with CAS, journaling each returned sequence number; preserve document IDs, source URLs, profiles and provenance.",
            "Clone the metadata generation, apply planned replacement projections, verify counts/DLS and atomically switch its alias; retain original generation.",
            "Verify user-scoped reads, source downloads and citations; move legacy originals into a hidden backup only after byte-for-byte verification.",
            "Resume using the validated authenticated deployment; verify forced password replacement before exposing normal access.",
        ],
        "rollback": [
            "Keep writes paused. Verify destination hashes and every post-write CAS recorded in the execution journal; abort if user data changed.",
            "Restore original metadata alias targets; keep the new generation for diagnosis.",
            "Restore only recorded ACL fields, including removal of fields absent before migration, guarded by journaled CAS.",
            "Restore legacy files from the retained originals/hidden backup; remove destination copies only after hash comparison.",
            "Remove only source locators and user-storage binding inserted by this run, or restore the consistent SQL backup before any new traffic.",
            "Retain the original auth boundary; never fall back from a live authenticated workspace to public no-auth access.",
        ],
    }


def main():
    import resource

    # Bound this maintenance helper in addition to its separate container/process limits.
    if hasattr(resource, "RLIMIT_AS") and os.uname().sysname == "Linux":
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    from dotenv import load_dotenv

    load_dotenv("/app/.env" if Path("/app/.env").is_file() else Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", required=True)
    parser.add_argument("--user-id")
    parser.add_argument(
        "--documents", default=os.getenv("OPENRAG_DOCUMENTS_PATH", "openrag-documents")
    )
    parser.add_argument("--archive", default=os.getenv("OPENRAG_INDEXED_DOCUMENTS_PATH"))
    parser.add_argument(
        "--database", default=str(Path(os.getenv("OPENRAG_DATA_PATH", "data")) / "openrag.db")
    )
    parser.add_argument("--index", default="documents")
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--output", default="-")
    args = parser.parse_args()
    args.archive = args.archive or str(Path(args.documents) / ".openrag-indexed")
    plan = build_plan(args, ReadOnlySearch() if args.include_index else None)
    payload = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        print(payload, end="")
    else:
        path = Path(args.output)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            output.write(payload)
        print(
            json.dumps(
                {
                    "plan": str(path),
                    "files": len(plan["files"]),
                    "chunks": len(plan["chunk_acl_changes"]),
                    "blockers": len(plan["blockers"]),
                    "user_id": plan["target"]["user_id"],
                }
            )
        )


if __name__ == "__main__":
    main()
