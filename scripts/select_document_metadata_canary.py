#!/usr/bin/env python3
"""Select a deterministic, size-adversarial metadata canary cohort."""

from __future__ import annotations

import bootstrap  # noqa: F401

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from config.settings import clients, get_index_name, get_indexed_documents_path
from services.document_metadata_backfill import (
    IndexedDocumentRecord,
    MetadataBackfillStatus,
    load_archive_manifest,
    map_archived_original,
    scan_indexed_documents,
)

FORMAT_QUOTAS = {
    "EML": 12,
    "PDF": 20,
    "DOCX": 10,
    "XLSX": 10,
    "IMAGE": 6,
    "HTML": 5,
    "CSV": 4,
    "TXT": 5,
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
TXT_SUFFIXES = {".txt", ".md", ".asc", ".adoc", ".asciidoc"}


def _format(record: IndexedDocumentRecord) -> str | None:
    suffix = Path(record.filename).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "IMAGE"
    if suffix in TXT_SUFFIXES:
        return "TXT"
    if suffix in {".html", ".htm"}:
        return "HTML"
    value = suffix.lstrip(".").upper()
    return value if value in FORMAT_QUOTAS else None


def _spread(values: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if len(values) <= count:
        return values
    # Sorted values yield actual small/median/large representatives rather
    # than a random sample that would systematically miss tail I/O cost.
    indexes = {round(position * (len(values) - 1) / (count - 1)) for position in range(count)}
    return [values[index] for index in sorted(indexes)]


def _atomic_private_json(path: Path, value: Any) -> None:
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


async def run(args: argparse.Namespace) -> int:
    manifest = load_archive_manifest(args.archive_manifest)
    client = clients.create_index_admin_opensearch_client()
    if client is None:
        raise RuntimeError("an index-admin OpenSearch client is required")
    candidates: list[dict[str, Any]] = []
    try:
        async for record in scan_indexed_documents(client, index_name=args.index, batch_size=500):
            format_name = _format(record)
            if format_name is None:
                continue
            decision = await asyncio.to_thread(
                map_archived_original,
                record,
                local_archive_root=args.local_archive_root,
                manifest=manifest,
                verify_local_hash=False,
            )
            if decision.status is not MetadataBackfillStatus.SUCCESS:
                continue
            size = (
                decision.manifest.size_bytes if decision.manifest is not None else record.file_size
            )
            if size is None or size < 0 or size > args.max_bytes:
                continue
            candidates.append(
                {
                    "record": record,
                    "format": format_name,
                    "size_bytes": size,
                    "archive_kind": decision.archive_source,
                    "is_attachment": record.source_entity_type == "email_attachment",
                }
            )
    finally:
        await client.close()

    by_format: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_format[candidate["format"]].append(candidate)
    for values in by_format.values():
        values.sort(
            key=lambda value: (
                value["size_bytes"],
                value["record"].source_entity_id,
                value["record"].storage_id,
            )
        )
    selected: dict[str, dict[str, Any]] = {}

    def include(values: list[dict[str, Any]]) -> None:
        for value in values:
            selected[value["record"].storage_id] = value

    for format_name, quota in FORMAT_QUOTAS.items():
        if len(by_format[format_name]) < quota:
            raise RuntimeError(
                f"insufficient mapped {format_name} candidates: "
                f"{len(by_format[format_name])} < {quota}"
            )
        include(_spread(by_format[format_name], quota))

    ordered_large = sorted(
        candidates,
        key=lambda value: (
            -value["size_bytes"],
            value["record"].source_entity_id,
            value["record"].storage_id,
        ),
    )
    include(ordered_large[:8])
    include([value for value in ordered_large if value["is_attachment"]][:12])
    include(
        [value for value in ordered_large if value["archive_kind"] == "openrag_local_archive"][:12]
    )
    for value in ordered_large:
        if len(selected) >= args.documents:
            break
        include([value])
    if not 50 <= len(selected) <= 150:
        raise RuntimeError(f"selected canary size is unsafe: {len(selected)}")

    final = sorted(
        selected.values(),
        key=lambda value: (
            value["format"],
            value["size_bytes"],
            value["record"].storage_id,
        ),
    )
    formats = Counter(value["format"] for value in final)
    archive_kinds = Counter(value["archive_kind"] for value in final)
    payload = {
        "schema": "openrag.document-metadata-canary-cohort",
        "version": 1,
        "selection": "deterministic format-stratified size spread plus largest tails",
        "index": args.index,
        "documents": len(final),
        "formats": dict(sorted(formats.items())),
        "archive_kinds": dict(sorted(archive_kinds.items())),
        "openarchiver_attachments": sum(value["is_attachment"] for value in final),
        "total_bytes": sum(value["size_bytes"] for value in final),
        "largest_files": [
            {
                "storage_id": value["record"].storage_id,
                "filename": value["record"].filename,
                "format": value["format"],
                "size_bytes": value["size_bytes"],
                "archive_kind": value["archive_kind"],
            }
            for value in sorted(final, key=lambda item: -item["size_bytes"])[:10]
        ],
        "records": [value["record"].model_dump(mode="json") for value in final],
    }
    _atomic_private_json(Path(args.output), payload)
    print(
        json.dumps(
            {key: payload[key] for key in payload if key != "records"},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select the production metadata canary cohort")
    parser.add_argument("--index", default=get_index_name())
    parser.add_argument("--archive-manifest", required=True)
    parser.add_argument("--local-archive-root", default=get_indexed_documents_path())
    parser.add_argument("--documents", type=int, default=100)
    parser.add_argument("--max-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 50 <= args.documents <= 150:
        parser.error("--documents must be between 50 and 150")
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
