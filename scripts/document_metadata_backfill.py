#!/usr/bin/env python3
"""Explicit, resumable operator CLI for document metadata audit/backfill."""

from __future__ import annotations

import bootstrap  # noqa: F401  # must load .env before settings imports

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import clients, get_index_name, get_indexed_documents_path
from services.document_metadata_backfill import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    ArchivedOriginalResolver,
    BackfillDocumentResult,
    DocumentMetadataBackfillJob,
    IndexedDocumentRecord,
    MappingDecision,
    MetadataBackfillStatus,
    bounded_process,
    load_archive_manifest,
    map_archived_original,
    scan_indexed_documents,
)

WRITE_CONFIRMATION = "openrag.document-metadata-v1"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _result_json(result: BackfillDocumentResult, *, include_profile: bool) -> dict[str, Any]:
    value: dict[str, Any] = {
        "occurrence_id": result.occurrence_id,
        "document_id": result.document_id,
        "status": result.status.value,
        "mapping_strength": result.mapping_strength.value,
        "reason": result.reason,
        "metadata_facts_sha256": result.metadata_facts_sha256,
        "format": result.format_name,
        "extraction_ms": result.extraction_ms,
        "bytes_read": result.bytes_read,
        "conflicts": result.conflicts,
        "chunks_matched": result.chunks_matched,
        "chunks_updated": result.chunks_updated,
    }
    if include_profile:
        value["profile"] = result.profile
    return value


def _mapping_result(
    record: IndexedDocumentRecord,
    decision: MappingDecision,
) -> BackfillDocumentResult:
    return BackfillDocumentResult(
        occurrence_id=record.occurrence_id,
        document_id=record.document_id,
        status=decision.status,
        mapping_strength=decision.strength,
        reason=decision.reason,
    )


def _summary(
    *,
    args: argparse.Namespace,
    started_at: str,
    finished_at: str,
    state: dict[str, Any],
    last_sort: list[Any] | None,
    scanned: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile": "openrag.document-metadata v1",
        "mode": _mode(args),
        "started_at": started_at,
        "finished_at": finished_at,
        "index": args.index,
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "scanned": scanned,
        "processed": state["processed"],
        "status_counts": dict(sorted(state["status_counts"].items())),
        "would_update": state["would_update"],
        "would_skip": state["would_skip"],
        "chunks_updated": state["chunks_updated"],
        "bytes_read": state["bytes_read"],
        "conflicts": state["conflicts"],
        "extraction_ms_total": round(state["extraction_ms_total"], 3),
        "mean_extraction_ms": (
            round(state["extraction_ms_total"] / state["extraction_samples"], 3)
            if state["extraction_samples"]
            else None
        ),
        "result_log": args.result_log,
        "last_sort": last_sort,
    }


def _empty_state() -> dict[str, Any]:
    return {
        "processed": 0,
        "status_counts": {},
        "would_update": 0,
        "would_skip": 0,
        "chunks_updated": 0,
        "bytes_read": 0,
        "conflicts": 0,
        "extraction_ms_total": 0.0,
        "extraction_samples": 0,
    }


def _mode(args: argparse.Namespace) -> str:
    return "AUDIT" if args.audit_only else ("WRITE" if args.write else "DRY_RUN")


def _restore_checkpoint(
    args: argparse.Namespace,
    checkpoint_path: Path,
    default_started_at: str,
) -> tuple[str, list[Any] | None, int, dict[str, Any]]:
    checkpoint = json.loads(checkpoint_path.read_text())
    if checkpoint.get("index") != args.index:
        raise ValueError("checkpoint index differs from requested index")
    if checkpoint.get("mode") != _mode(args):
        raise ValueError("checkpoint mode differs from requested mode")
    restored = checkpoint.get("checkpoint_state")
    if not isinstance(restored, dict):
        raise ValueError("checkpoint has no bounded accumulator state")
    state = {**_empty_state(), **restored}
    return (
        str(checkpoint.get("started_at") or default_started_at),
        checkpoint.get("last_sort"),
        int(checkpoint.get("scanned", 0)),
        state,
    )


def _accumulate(state: dict[str, Any], results: list[dict[str, Any]]) -> None:
    for item in results:
        state["processed"] += 1
        status = str(item["status"])
        state["status_counts"][status] = state["status_counts"].get(status, 0) + 1
        if status == MetadataBackfillStatus.SUCCESS.value:
            if item["reason"] == "dry_run_would_update":
                state["would_update"] += 1
        else:
            state["would_skip"] += 1
        state["chunks_updated"] += int(item.get("chunks_updated") or 0)
        state["bytes_read"] += int(item.get("bytes_read") or 0)
        state["conflicts"] += int(item.get("conflicts") or 0)
        if item.get("extraction_ms") is not None:
            state["extraction_ms_total"] += float(item["extraction_ms"])
            state["extraction_samples"] += 1


def _append_result_log(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for item in results:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


async def run(args: argparse.Namespace) -> int:
    manifest = load_archive_manifest(args.archive_manifest) if args.archive_manifest else {}
    client = clients.create_index_admin_opensearch_client()
    if client is None:
        raise RuntimeError("an index-admin OpenSearch client is required")
    resolver = ArchivedOriginalResolver(
        openarchiver_base_url=args.openarchiver_base_url,
        openarchiver_api_key_file=args.openarchiver_api_key_file,
        temporary_directory=args.temporary_directory,
        timeout_seconds=args.archive_timeout,
        download_attempts=args.archive_retries,
    )
    job = DocumentMetadataBackfillJob(
        client,
        index_name=args.index,
        manifest=manifest,
        local_archive_root=args.local_archive_root,
        resolver=resolver,
        write=args.write,
    )
    await job.ensure_mapping()
    started_at = datetime.now(UTC).isoformat()
    checkpoint_path = Path(args.checkpoint)
    output_path = Path(args.output)
    last_sort: list[Any] | None = None
    state = _empty_state()
    scanned = 0
    if args.resume and checkpoint_path.exists():
        started_at, last_sort, scanned, state = _restore_checkpoint(
            args,
            checkpoint_path,
            started_at,
        )
    elif Path(args.result_log).exists():
        Path(args.result_log).unlink()

    selected_ids = set(args.document_id or [])
    selected_entities = set(args.entity_id or [])
    selected_suffixes = {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in (args.extension or [])
    }
    batch: list[IndexedDocumentRecord] = []
    try:
        async for record in scan_indexed_documents(
            client,
            index_name=args.index,
            batch_size=args.batch_size,
            search_after=last_sort,
        ):
            if args.limit and state["processed"] >= args.limit:
                break
            scanned += 1
            last_sort = record.sort
            if selected_ids and record.document_id not in selected_ids:
                continue
            if selected_entities and record.source_entity_id not in selected_entities:
                continue
            suffix = Path(record.filename).suffix.lower()
            if selected_suffixes and suffix not in selected_suffixes:
                continue
            batch.append(record)
            remaining = args.limit - state["processed"] if args.limit else args.batch_size
            if len(batch) < min(args.batch_size, remaining):
                continue
            batch_results = await _process_batch(job, batch, args)
            batch_json = [
                _result_json(item, include_profile=args.include_profiles) for item in batch_results
            ]
            _append_result_log(Path(args.result_log), batch_json)
            _accumulate(state, batch_json)
            batch = []
            _checkpoint(args, started_at, state, last_sort, scanned)
            if args.limit and state["processed"] >= args.limit:
                break
        if batch and (not args.limit or state["processed"] < args.limit):
            if args.limit:
                batch = batch[: max(0, args.limit - state["processed"])]
            batch_results = await _process_batch(job, batch, args)
            batch_json = [
                _result_json(item, include_profile=args.include_profiles) for item in batch_results
            ]
            _append_result_log(Path(args.result_log), batch_json)
            _accumulate(state, batch_json)
        finished_at = datetime.now(UTC).isoformat()
        report = _summary(
            args=args,
            started_at=started_at,
            finished_at=finished_at,
            state=state,
            last_sort=last_sort,
            scanned=scanned,
        )
        report["checkpoint_state"] = state
        _atomic_json(output_path, report)
        _atomic_json(checkpoint_path, report)
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "mode",
                        "scanned",
                        "processed",
                        "status_counts",
                        "would_update",
                        "would_skip",
                        "chunks_updated",
                        "bytes_read",
                    )
                },
                indent=2,
            )
        )
        return 0
    finally:
        await client.close()


async def _process_batch(
    job: DocumentMetadataBackfillJob,
    batch: list[IndexedDocumentRecord],
    args: argparse.Namespace,
) -> list[BackfillDocumentResult]:
    if args.audit_only:
        decisions = await asyncio.gather(
            *(
                asyncio.to_thread(
                    map_archived_original,
                    record,
                    local_archive_root=args.local_archive_root,
                    manifest=job.manifest,
                )
                for record in batch
            )
        )
        return [
            _mapping_result(record, decision)
            for record, decision in zip(batch, decisions, strict=True)
        ]
    return await bounded_process(job, batch, concurrency=args.concurrency)


def _checkpoint(
    args: argparse.Namespace,
    started_at: str,
    state: dict[str, Any],
    last_sort: list[Any] | None,
    scanned: int,
) -> None:
    value = _summary(
        args=args,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        state=state,
        last_sort=last_sort,
        scanned=scanned,
    )
    value["checkpoint_state"] = state
    _atomic_json(Path(args.checkpoint), value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or backfill openrag.document-metadata v1 without reingestion"
    )
    parser.add_argument("--index", default=get_index_name())
    parser.add_argument("--local-archive-root", default=get_indexed_documents_path())
    parser.add_argument("--archive-manifest")
    parser.add_argument("--openarchiver-base-url")
    parser.add_argument("--openarchiver-api-key-file")
    parser.add_argument("--temporary-directory")
    parser.add_argument("--archive-timeout", type=float, default=120)
    parser.add_argument("--archive-retries", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--document-id", action="append")
    parser.add_argument("--entity-id", action="append")
    parser.add_argument("--extension", action="append")
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument(
        "--result-log",
        help="append-only JSONL document status log (defaults beside --output)",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--include-profiles", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm-profile", default="")
    args = parser.parse_args()
    if args.write and args.audit_only:
        parser.error("--write cannot be combined with --audit-only")
    if args.write and args.confirm_profile != WRITE_CONFIRMATION:
        parser.error(f"--write requires --confirm-profile {WRITE_CONFIRMATION}")
    if args.concurrency < 1 or args.concurrency > 8:
        parser.error("--concurrency must be between 1 and 8")
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("--batch-size must be between 1 and 1000")
    if args.archive_retries < 1 or args.archive_retries > 5:
        parser.error("--archive-retries must be between 1 and 5")
    if args.include_profiles and (not args.limit or args.limit > 100):
        parser.error("--include-profiles requires a --limit between 1 and 100")
    if not args.result_log:
        args.result_log = str(Path(args.output).with_suffix(".results.jsonl"))
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
