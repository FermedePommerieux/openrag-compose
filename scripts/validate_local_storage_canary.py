"""Exercise migration on two read-only originals copied into /tmp only.

Requires the isolated two-user validation DB and its result JSON. Never creates
production accounts, moves originals, changes production ACLs or switches aliases.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def verify_original(record):
    path = Path(record["source"])
    assert path.is_relative_to("/shared/openrag-documents")
    assert path.resolve() == path and not path.is_symlink()
    assert os.statvfs(path).f_flag & os.ST_RDONLY, "Original volume must be read-only"
    stat = path.stat()
    assert (stat.st_size, stat.st_mtime_ns, stat.st_ino) == (
        record["bytes"],
        record["mtime_ns"],
        record["inode"],
    ), "Original drifted; recapture manifest"
    assert file_hash(path) == record["sha256"], "Original hash drifted"
    return path


def verified_copy(record, destination):
    source = verify_original(record)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as original, destination.open("xb") as copy:
        shutil.copyfileobj(original, copy, length=1024 * 1024)
    assert file_hash(destination) == record["sha256"]


async def validate(args):
    scratch = Path(args.scratch).resolve()
    database = Path(args.database).resolve()
    assert scratch.is_relative_to("/tmp") and not scratch.exists()
    assert database.is_relative_to("/tmp") and database.name == "controlled-users.db"
    manifest = json.loads(Path(args.manifest).read_text())
    evidence = json.loads(Path(args.validation).read_text())
    assert evidence["status"] == "PASS" and len(manifest["files"]) == 2
    root = scratch / "documents"
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "OPENRAG_DATA_PATH": str(scratch / "data"),
            "OPENRAG_CONFIG_PATH": str(scratch / "config"),
            "OPENRAG_KEYS_PATH": args.keys,
            "OPENRAG_DOCUMENTS_PATH": str(root),
            "OPENRAG_INDEXED_DOCUMENTS_PATH": str(root / ".openrag-indexed"),
            "OPENRAG_AUTH_MODE": "local",
            "OPENRAG_RBAC_ENFORCE": "true",
            "IBM_AUTH_ENABLED": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )
    from config import settings
    from db import engine
    from db.models import LocalCredential, User
    from services.dls_principal_service import DLSPrincipalService
    from services.local_auth_service import issue_session
    from services.local_source_service import (
        LocalSourceNotFoundError,
        local_source_url,
        resolve_local_source_download,
        stage_local_source,
    )
    from services.user_storage_service import (
        archive_root_for_source,
        get_user_storage,
        register_archive,
        unregister_archive,
    )
    from session_manager import SessionManager

    engine.init_engine()
    manager = SessionManager()
    admin = settings.clients.create_index_admin_opensearch_client()
    dls = DLSPrincipalService(None, opensearch_client=admin)
    clients = {}
    users = {side: evidence["users"][side]["user_id"] for side in ["A", "B"]}
    index = "documents_storage_canary_" + uuid.uuid4().hex
    created = False
    registered = None
    staged = None
    checks = []
    result = {"status": "FAIL", "production_mutated": False, "checks": checks}
    try:
        assert engine.SessionLocal is not None
        async with engine.SessionLocal() as session:
            for side, uid in users.items():
                user = await session.get(User, uid)
                credential = await session.get(LocalCredential, uid)
                assert user and user.oauth_provider == "local"
                assert credential and credential.login == "user-" + side.lower()
                token, principal = await issue_session(session, manager, user, ttl_seconds=300)
                await dls.refresh_user_principals(principal)
                clients[side] = settings.clients.create_user_opensearch_client(token)
            await session.commit()
        storage = await get_user_storage(users["A"])
        foreign = await get_user_storage(users["B"])
        assert storage.archive.is_relative_to(root) and foreign.archive != storage.archive
        await admin.indices.create(
            index=index,
            body={
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {
                    "properties": {
                        field: {"type": "keyword"}
                        for field in [
                            "document_id",
                            "source_url",
                            "owner",
                            "allowed_users",
                            "allowed_groups",
                            "allowed_principals",
                        ]
                    }
                },
            },
        )
        created = True
        archive = next(r for r in manifest["files"] if r["kind"] == "archives")
        source_id = archive["source_id"]
        destination = storage.archive / source_id / Path(archive["source"]).name
        verified_copy(archive, destination)
        await register_archive(source_id, users["A"])
        registered = source_id
        url = local_source_url(source_id)
        assert url.endswith("/api/source-files/" + source_id)
        await admin.index(
            index=index,
            id="archive-copy",
            body={
                "document_id": archive["document_id"],
                "source_url": url,
                "owner": users["A"],
                "allowed_users": [users["A"]],
                "allowed_groups": [],
                "allowed_principals": [],
            },
            refresh=True,
        )
        own = await resolve_local_source_download(
            source_id, opensearch_client=clients["A"], index=index
        )
        assert own.path == destination and file_hash(own.path) == archive["sha256"]
        try:
            await resolve_local_source_download(
                source_id, opensearch_client=clients["B"], index=index
            )
        except LocalSourceNotFoundError:
            pass
        else:
            raise AssertionError("Other reader reached migrated archive")
        checks.extend(
            [
                "archive_copy_hash",
                "existing_source_id_and_url_preserved",
                "real_owner_download",
                "real_other_reader_denied",
            ]
        )
        ingestion = next(r for r in manifest["files"] if r["kind"] == "ingestion")
        inbox = storage.ingestion / Path(ingestion["source"]).name
        verified_copy(ingestion, inbox)
        staged = await stage_local_source(
            inbox, ingestion["document_id"], inbox.name, owner_user_id=users["A"]
        )
        assert staged.archived_path.is_relative_to(storage.archive) and not inbox.exists()
        assert file_hash(staged.archived_path) == ingestion["sha256"]
        assert await archive_root_for_source(staged.source_id) == storage.archive
        staged_id = staged.source_id
        await staged.rollback()
        staged = None
        assert file_hash(inbox) == ingestion["sha256"]
        assert await archive_root_for_source(staged_id) is None
        checks.extend(["owner_ingestion_archive", "archive_rollback_hash_and_locator"])
        await unregister_archive(source_id)
        registered = None
        assert await archive_root_for_source(source_id) is None
        for record in manifest["files"]:
            verify_original(record)
        checks.extend(["migration_locator_rollback", "all_originals_unchanged"])
        result["status"] = "PASS"
    finally:
        if staged:
            await staged.rollback()
        if registered:
            await unregister_archive(registered)
        if created:
            await admin.indices.delete(index=index)
        for uid in users.values():
            await admin.delete(
                index=settings.DLS_PRINCIPAL_INDEX_NAME, id=uid, ignore=[404], refresh=True
            )
        for client in clients.values():
            await client.close()
        await admin.close()
        await engine.dispose_engine()
        if scratch.exists():
            shutil.rmtree(scratch)
        result["canary_index_cleaned"] = True
        result["scratch_copies_cleaned"] = not scratch.exists()
        print("STORAGE_CANARY=" + json.dumps(result), flush=True)
    return result["status"] == "PASS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["manifest", "validation", "database", "scratch", "keys"]:
        parser.add_argument("--" + name, required=True)
    return 0 if asyncio.run(validate(parser.parse_args())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
