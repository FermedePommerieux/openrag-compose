"""Stream a read-only file/ACL inventory as JSONL from an isolated maintenance pod.

Never run in the serving backend. Every production volume must be read-only.
No credentials, document text, metadata payloads or private keys enter output.
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
import stat
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE = re.compile(r"^(?P<document>[A-Za-z0-9_-]{16,128})\.[a-f0-9]{32}$")
ACL = (
    "owner",
    "owner_name",
    "owner_email",
    "allowed_users",
    "allowed_groups",
    "allowed_principals",
)


def emit(event, **values):
    print(
        json.dumps({"event": event, **values}, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Search:
    def __init__(self):
        self.base = f"https://{os.getenv('OPENSEARCH_HOST', 'localhost')}:{os.getenv('OPENSEARCH_PORT', '9200')}"
        secret = os.getenv("OPENSEARCH_USERNAME", "admin") + ":" + os.environ["OPENSEARCH_PASSWORD"]
        self.headers = {
            "Authorization": "Basic " + base64.b64encode(secret.encode()).decode(),
            "Content-Type": "application/json",
        }

    def request(self, path, body=None, method=None):
        if not (
            re.fullmatch(r"/[a-z0-9_.-]+/_search\?scroll=2m", path)
            or path == "/_search/scroll"
            or re.fullmatch(r"/_alias/[a-z0-9_.-]+", path)
        ):
            raise ValueError("Read-only inventory refuses endpoint")
        req = Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=self.headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        with urlopen(req, context=ssl._create_unverified_context(), timeout=45) as response:
            value = response.read(2 * 1024 * 1024 + 1)
        if len(value) > 2 * 1024 * 1024:
            raise ValueError("Inventory response exceeded 2 MiB; reduce page size")
        return json.loads(value)

    def scan(self, index, fields, query, page_size=250):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", index):
            raise ValueError("Exact index name required")
        scroll = None
        observed = 0
        expected = None
        try:
            page = self.request(
                "/" + index + "/_search?scroll=2m",
                {
                    "query": query,
                    "size": page_size,
                    "sort": ["_doc"],
                    "_source": fields,
                    "seq_no_primary_term": True,
                    "track_total_hits": True,
                },
            )
            while True:
                scroll = page.get("_scroll_id", scroll)
                shards = page.get("_shards", {})
                if (
                    page.get("timed_out")
                    or page.get("terminated_early")
                    or shards.get("failed", 0)
                    or shards.get("successful", 0) != shards.get("total", -1)
                ):
                    raise ValueError("Incomplete inventory search execution")
                hits = page.get("hits", {}).get("hits", [])
                total = page.get("hits", {}).get("total", {})
                if not isinstance(total, dict) or total.get("relation") != "eq":
                    raise ValueError("Exact inventory hit total required")
                if expected is None:
                    expected = total["value"]
                if total["value"] != expected:
                    raise ValueError("Inventory total changed during scroll")
                if not hits:
                    if observed != expected:
                        raise ValueError("Inventory scroll ended before its exact total")
                    break
                observed += len(hits)
                if observed > expected:
                    raise ValueError("Inventory exceeded its exact total")
                yield from hits
                if not scroll:
                    break
                page = self.request("/_search/scroll", {"scroll": "2m", "scroll_id": scroll})
        finally:
            if scroll:
                self.request("/_search/scroll", {"scroll_id": [scroll]}, "DELETE")


def legacy_acl_reason(source):
    if source.get("owner") not in (None, "", "anonymous"):
        return "existing_other_owner"
    if source.get("connector_type") not in (None, "local"):
        return "non_local_connector"
    if (
        source.get("allowed_groups")
        or source.get("allowed_principals")
        or any(x != "anonymous" for x in (source.get("allowed_users") or []))
    ):
        return "explicit_sharing_requires_review"
    return None


def main():
    from dotenv import load_dotenv

    load_dotenv("/app/.env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-user-id", required=True)
    parser.add_argument("--login", default="eloiprimaux")
    parser.add_argument(
        "--documents", default=os.getenv("OPENRAG_DOCUMENTS_PATH", "/shared/openrag-documents")
    )
    parser.add_argument(
        "--archive",
        default=os.getenv(
            "OPENRAG_INDEXED_DOCUMENTS_PATH", "/shared/openrag-documents/.openrag-indexed"
        ),
    )
    parser.add_argument("--database", default="/data/data/openrag.db")
    parser.add_argument("--index", default="documents")
    args = parser.parse_args()
    emit(
        "start",
        schema="openrag.storage-inventory.v2",
        captured_at=time.time(),
        target={"login": args.login, "user_id": args.target_user_id},
        read_only=True,
    )
    with sqlite3.connect(Path(args.database).as_uri() + "?mode=ro", uri=True) as db:
        known_users = dict(db.execute("SELECT id,oauth_provider FROM users"))
        managed = (
            [r[0] for r in db.execute("SELECT directory FROM user_storage")]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='user_storage'").fetchone()
            else []
        )
    base, archive = Path(args.documents), Path(args.archive)
    if base.is_symlink() or archive.is_symlink():
        raise ValueError("Symlinked inventory root")
    files = []
    statuses = {}
    for kind, root in [("ingestion", base), ("archives", archive)]:
        if not root.exists():
            continue
        for current_name, dirs, names in os.walk(root, followlinks=False):
            current = Path(current_name)
            dirs[:] = sorted(
                d for d in dirs if current / d != archive and not (current == base and d in managed)
            )
            for name in list(dirs):
                if (current / name).is_symlink():
                    emit("unknown_path", path=str(current / name), reason="symlink_directory")
                    dirs.remove(name)
            for name in sorted(names):
                path = current / name
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    emit("unknown_path", path=str(path), reason="non_regular_file")
                    continue
                sha = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        sha.update(chunk)
                    if hasattr(os, "posix_fadvise"):
                        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                after = path.stat()
                docid = base64.urlsafe_b64encode(sha.digest()).decode().rstrip("=")[:24]
                relative = path.relative_to(root)
                source_id = relative.parts[0] if kind == "archives" else None
                reasons = []
                if source_id and (len(relative.parts) != 2 or not SOURCE.fullmatch(source_id)):
                    reasons.append("unrecognized_archive_layout")
                elif source_id and SOURCE.fullmatch(source_id).group("document") != docid:
                    reasons.append("archive_content_id_mismatch")
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ):
                    reasons.append("file_changed_during_hash")
                item = {
                    "file_id": digest(str(path)),
                    "kind": kind,
                    "source": str(path),
                    "destination": str(base / args.login / kind / relative),
                    "bytes": after.st_size,
                    "sha256": sha.hexdigest(),
                    "mtime_ns": after.st_mtime_ns,
                    "inode": after.st_ino,
                    "mode": oct(after.st_mode & 0o777),
                    "uid": after.st_uid,
                    "gid": after.st_gid,
                    "document_id": docid,
                    "source_id": source_id,
                    "reasons": reasons,
                }
                files.append(item)
                statuses.setdefault(docid, {"chunks": 0, "owners": set(), "reasons": set()})
                emit("file", **item)
                if len(files) % 25 == 0:
                    emit("progress", files=len(files))
    search = Search()
    chunks = 0
    indexed_documents = {}
    for hit in search.scan(
        args.index,
        list(ACL) + ["document_id", "source_url", "connector_type", "chunk_index"],
        {"match_all": {}},
    ):
        source = hit["_source"]
        docid = source.get("document_id")
        chunks += 1
        reason = legacy_acl_reason(source)
        owner = source.get("owner")
        if docid in statuses:
            state = statuses[docid]
            state["chunks"] += 1
            state["owners"].add(owner)
            if reason:
                state["reasons"].add(reason)
            proposed = args.target_user_id if not reason else None
            assignment = "ASSIGNED" if proposed else "UNKNOWN"
        elif owner and owner != "anonymous" and owner in known_users:
            proposed, assignment = owner, "KEEP_EXISTING_OWNER"
        else:
            proposed, assignment = None, "UNKNOWN"
            reason = "indexed_document_without_physical_file_or_known_owner"
        summary = indexed_documents.setdefault(
            docid, {"chunks": 0, "owners": set(), "reasons": set(), "assignments": set()}
        )
        if len(indexed_documents) > 100000:
            raise ValueError(
                "Inventory document summary limit reached; split into disjoint batches"
            )
        summary["chunks"] += 1
        summary["owners"].add(owner)
        summary["assignments"].add(assignment)
        if assignment == "UNKNOWN":
            summary["reasons"].add(reason)
        emit(
            "chunk_acl",
            index=hit["_index"],
            id=hit["_id"],
            document_id=docid,
            source_url=source.get("source_url"),
            connector_type=source.get("connector_type"),
            if_seq_no=hit["_seq_no"],
            if_primary_term=hit["_primary_term"],
            before={k: source[k] for k in ACL if k in source},
            before_absent=[k for k in ACL if k not in source],
            assignment=assignment,
            proposed_owner=proposed,
            reason=reason if assignment == "UNKNOWN" else None,
        )
        if chunks % 5000 == 0:
            emit("progress", chunks=chunks, indexed_documents=len(indexed_documents))
    alias = "documents-metadata-filter-current"
    try:
        targets = sorted(search.request("/_alias/" + alias))
    except Exception as error:
        if getattr(error, "code", None) != 404:
            raise
        targets = []
    emit("metadata_alias", alias=alias, targets=targets)
    metadata_count = 0
    if targets and statuses:
        fields = [
            "projection_document_id",
            "source_document_id",
            "source_entity_id",
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
            alias, fields, {"terms": {"source_document_id": sorted(statuses)}}, page_size=25
        ):
            source = hit["_source"]
            docid = source["source_document_id"]
            reason = legacy_acl_reason(source)
            if reason:
                statuses[docid]["reasons"].add("metadata_" + reason)
            metadata_count += 1
            proposed_id = (
                digest(
                    {
                        "contract": source["filter"]["contract"],
                        "owner": args.target_user_id,
                        "source_document_id": docid,
                        "source_entity_id": source["source_entity_id"],
                    }
                )
                if not reason
                else None
            )
            emit(
                "metadata_acl",
                index=hit["_index"],
                id=hit["_id"],
                if_seq_no=hit["_seq_no"],
                if_primary_term=hit["_primary_term"],
                before=source,
                proposed_owner=args.target_user_id if not reason else None,
                proposed_id=proposed_id,
                reason=reason,
            )
    counts = Counter()
    for item in files:
        state = statuses[item["document_id"]]
        reasons = sorted(set(item["reasons"]) | state["reasons"])
        current = Path(item["source"]).stat()
        if (current.st_size, current.st_mtime_ns, current.st_ino) != (
            item["bytes"],
            item["mtime_ns"],
            item["inode"],
        ):
            reasons.append("file_changed_after_inventory")
        assignment = "UNKNOWN" if reasons else "ASSIGNED"
        counts[assignment] += 1
        emit(
            "file_owner",
            file_id=item["file_id"],
            source=item["source"],
            document_id=item["document_id"],
            assignment=assignment,
            owner_user_id=args.target_user_id if not reasons else None,
            owner_login=args.login if not reasons else None,
            basis="explicit_user_request_to_migrate_legacy_storage" if not reasons else None,
            indexed_chunks=state["chunks"],
            observed_owners=sorted(state["owners"], key=str),
            reasons=reasons,
        )
    for docid, state in indexed_documents.items():
        emit(
            "indexed_document_owner",
            document_id=docid,
            chunks=state["chunks"],
            owners=sorted(state["owners"], key=str),
            assignments=sorted(state["assignments"]),
            reasons=sorted(state["reasons"]),
        )
    emit(
        "complete",
        files=len(files),
        bytes=sum(f["bytes"] for f in files),
        archive_files=sum(f["kind"] == "archives" for f in files),
        chunks=chunks,
        indexed_documents=len(indexed_documents),
        metadata_rows=metadata_count,
        file_assignments=dict(counts),
        captured_at=time.time(),
        quiesced=False,
        production_mutated=False,
    )


if __name__ == "__main__":
    main()
