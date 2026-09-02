#!/usr/bin/env python3
"""Operator-only production canary; never scans or writes beyond its cohort file."""

from __future__ import annotations

import bootstrap  # noqa: F401

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from config.settings import clients, get_index_name, get_indexed_documents_path
from services.document_metadata_backfill import (
    ArchivedOriginalResolver,
    IndexedDocumentRecord,
    load_archive_manifest,
)
from services.document_metadata_canary import CanaryCheckpoint, DocumentMetadataCanary

WRITE_CONFIRMATION = "openrag.document-metadata-canary-v1"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_cohort(path: Path) -> list[IndexedDocumentRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(values, list) or not 50 <= len(values) <= 150:
        raise ValueError("canary cohort must contain between 50 and 150 records")
    records = [IndexedDocumentRecord.model_validate(value) for value in values]
    storage_ids = [record.storage_id for record in records]
    if any(not value for value in storage_ids) or len(storage_ids) != len(set(storage_ids)):
        raise ValueError("canary cohort requires unique representative storage ids")
    return records


async def run(args: argparse.Namespace) -> int:
    records = _load_cohort(Path(args.cohort))
    manifest = load_archive_manifest(args.archive_manifest) if args.archive_manifest else {}
    client = clients.create_index_admin_opensearch_client()
    if client is None:
        raise RuntimeError("an index-admin OpenSearch client is required")
    checkpoint = CanaryCheckpoint(args.checkpoint)
    checkpoint.initialize(
        index_name=args.index,
        records=records,
        run_id=args.run_id or uuid.uuid4().hex,
    )
    resolver = ArchivedOriginalResolver(
        openarchiver_base_url=args.openarchiver_base_url,
        openarchiver_api_key_file=args.openarchiver_api_key_file,
        temporary_directory=args.temporary_directory,
        timeout_seconds=args.archive_timeout,
        download_attempts=args.archive_retries,
    )
    canary = DocumentMetadataCanary(
        client,
        index_name=args.index,
        checkpoint=checkpoint,
        manifest=manifest,
        local_archive_root=args.local_archive_root,
        resolver=resolver,
    )
    try:
        if args.rollback_count:
            candidates = [
                storage_id
                for storage_id, item in checkpoint.data["items"].items()
                if item.get("changed") is True
                and item.get("state") in {"VERIFIED", "WRITTEN", "FAILED"}
            ][: args.rollback_count]
            result = await canary.rollback(candidates)
            summary = {**canary.summary(), "rollback": result, "rollback_targets": candidates}
        else:
            summary = await canary.run()
        _atomic_json(Path(args.output), summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a 50-150 occurrence metadata-only production canary"
    )
    parser.add_argument("--index", default=get_index_name())
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--archive-manifest")
    parser.add_argument("--local-archive-root", default=get_indexed_documents_path())
    parser.add_argument("--openarchiver-base-url")
    parser.add_argument("--openarchiver-api-key-file")
    parser.add_argument("--temporary-directory")
    parser.add_argument("--archive-timeout", type=float, default=120)
    parser.add_argument("--archive-retries", type=int, default=3)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--rollback-count", type=int, default=0)
    parser.add_argument("--confirm-canary", default="")
    args = parser.parse_args()
    if args.confirm_canary != WRITE_CONFIRMATION:
        parser.error(f"production writes require --confirm-canary {WRITE_CONFIRMATION}")
    if args.rollback_count < 0 or args.rollback_count > 150:
        parser.error("--rollback-count must be between 0 and 150")
    if args.archive_retries < 1 or args.archive_retries > 5:
        parser.error("--archive-retries must be between 1 and 5")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
