"""Read-only DLS corpus snapshot collection through the public files API."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


def _default_get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=120) as response:  # noqa: S310 - operator-supplied benchmark URL
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("files API response must be an object")
    return value


def snapshot_visible_corpus(
    base_url: str,
    *,
    page_size: int = 1000,
    get_json: Callable[[str], dict[str, Any]] = _default_get_json,
) -> dict[str, Any]:
    """Enumerate representative chunks under the caller's DLS context.

    The files endpoint uses ``chunk_index=0`` and search-after pagination, so
    each returned row is one visible source occurrence rather than one chunk.
    """
    page = 1
    cursor: str | None = None
    records: list[dict[str, Any]] = []
    expected_total: int | None = None
    while True:
        query: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "sort_by": "filename",
            "sort_order": "asc",
        }
        if cursor:
            query["cursor"] = cursor
        payload = get_json(f"{base_url.rstrip('/')}/api/files?{urlencode(query)}")
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        total = int(payload.get("total", 0))
        expected_total = total if expected_total is None else expected_total
        page_blocks = [payload, *payload.get("prefetched_pages", [])]
        last_page = page
        next_cursor = None
        for block in page_blocks:
            block_files = block.get("files", [])
            if isinstance(block_files, list):
                records.extend(item for item in block_files if isinstance(item, dict))
            last_page = int(block.get("page", last_page))
            next_cursor = block.get("next_cursor")
        if len(records) >= total or not next_cursor:
            break
        page = last_page + 1
        cursor = str(next_cursor)

    occurrence_keys = sorted(
        str(item.get("source_entity_id") or item.get("document_id") or "") for item in records
    )
    occurrence_keys = [key for key in occurrence_keys if key]
    digest = hashlib.sha256("\n".join(occurrence_keys).encode()).hexdigest()
    return {
        "visible_occurrences": expected_total or 0,
        "enumerated_occurrences": len(records),
        "distinct_document_ids": len(
            {str(item["document_id"]) for item in records if item.get("document_id")}
        ),
        "distinct_source_entity_ids": len(
            {str(item["source_entity_id"]) for item in records if item.get("source_entity_id")}
        ),
        "sources": sorted(
            {
                str(item.get("source_entity_system") or item.get("connector_type"))
                for item in records
                if item.get("source_entity_system") or item.get("connector_type")
            }
        ),
        "occurrence_identity_sha256": digest,
        "complete": len(records) == (expected_total or 0),
    }


def corpus_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Return True if either exact count or enumerated identity set changed."""
    return any(
        before.get(field) != after.get(field)
        for field in ("visible_occurrences", "occurrence_identity_sha256")
    )
