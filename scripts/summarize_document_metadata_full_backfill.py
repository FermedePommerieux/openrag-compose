#!/usr/bin/env python3
"""Aggregate private full-backfill checkpoints into non-identifying evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"VERIFIED", "FAILED", "DLS_BLOCKED", "ARCHIVE_UNAVAILABLE"}
REPORT_FIELDS = (
    "embedded_created_at",
    "embedded_modified_at",
    "creator",
    "last_modified_by",
    "producer",
    "creator_application",
    "archive_metadata",
    "parent_entity_ids",
    "filesystem_metadata",
    "conflicts",
    "timezone_unknown",
    "invalid_timestamps",
)


def _atomic_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _record_ref(item: dict[str, Any]) -> str:
    record = item.get("record") or {}
    material = "\0".join(
        str(record.get(field) or "") for field in ("storage_id", "document_id", "source_entity_id")
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _profile(item: dict[str, Any]) -> dict[str, Any] | None:
    for state_name in ("expected_metadata", "post_metadata", "pre_metadata"):
        state = item.get(state_name) or {}
        value = (state.get("present") or {}).get("document_metadata_profile")
        if isinstance(value, dict):
            return value
    return None


def _observations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for section in ("identity", "embedded", "filesystem", "archive", "ingestion"):
        result.extend(
            value for value in profile.get(section, []) if isinstance(value, dict)
        )
    return result


def _values(profile: dict[str, Any], field: str, section: str) -> set[str]:
    values: set[str] = set()
    for item in profile.get(section, []):
        if not isinstance(item, dict) or item.get("field") != field:
            continue
        value = item.get("value") if item.get("value") is not None else item.get("raw_value")
        if isinstance(value, list):
            values.update(str(entry) for entry in value)
        elif value is not None:
            values.add(str(value))
    return values


def _coverage_flags(profile: dict[str, Any]) -> dict[str, bool]:
    observations = _observations(profile)
    fields = {str(item.get("field")) for item in observations}
    return {
        "embedded_created_at": "embedded_created_at" in fields,
        "embedded_modified_at": "embedded_modified_at" in fields,
        "creator": "creator" in fields or "author" in fields,
        "last_modified_by": "last_modified_by" in fields,
        "producer": "producer" in fields,
        "creator_application": "creator_application" in fields,
        "archive_metadata": bool(profile.get("archive")),
        "parent_entity_ids": "parent_entity_ids" in fields,
        "filesystem_metadata": bool(profile.get("filesystem")),
        "conflicts": bool(profile.get("conflicts")),
        "timezone_unknown": any(item.get("timezone") == "UNKNOWN" for item in observations),
        "invalid_timestamps": any(
            item.get("normalization_status") == "invalid" for item in observations
        ),
    }


def _conflict_flags(profile: dict[str, Any]) -> dict[str, bool]:
    pdf_info_xmp = False
    for conflict in profile.get("conflicts", []):
        sources = {str(value) for value in conflict.get("sources", [])}
        if any("pdf_info" in value for value in sources) and any(
            "xmp" in value for value in sources
        ):
            pdf_info_xmp = True
    embedded_dates = _values(profile, "embedded_created_at", "embedded") | _values(
        profile, "embedded_modified_at", "embedded"
    )
    archive_dates = _values(profile, "archive_created_at", "archive") | _values(
        profile, "archive_modified_at", "archive"
    )
    creators = _values(profile, "creator", "embedded") | _values(
        profile, "author", "embedded"
    )
    modifiers = _values(profile, "last_modified_by", "embedded")
    observations = _observations(profile)
    return {
        "pdf_info_vs_xmp": pdf_info_xmp,
        "embedded_vs_archive_date": bool(
            embedded_dates and archive_dates and embedded_dates != archive_dates
        ),
        "creator_vs_last_modified_by": bool(
            creators and modifiers and creators != modifiers
        ),
        "invalid_timestamps": any(
            item.get("normalization_status") == "invalid" for item in observations
        ),
        "missing_timezone": any(item.get("timezone") == "UNKNOWN" for item in observations),
    }


def summarize(checkpoint_directory: Path) -> dict[str, Any]:
    batch_paths = sorted((checkpoint_directory / "batches").glob("batch-*.json"))
    state_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    format_counts: Counter[str] = Counter()
    coverage: dict[str, Counter[str]] = defaultdict(Counter)
    conflicts: Counter[str] = Counter()
    unsupported_formats: Counter[str] = Counter()
    remaining: list[dict[str, str]] = []
    profile_count = 0
    observations = 0
    for path in batch_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("items", {}).values():
            state = str(item.get("state") or "")
            if state not in TERMINAL_STATES:
                continue
            changed = item.get("changed") is True
            reported_state = "UNCHANGED" if state == "VERIFIED" and not changed else state
            state_counts[reported_state] += 1
            reason = str(item.get("reason") or "unspecified")
            if state in {"FAILED", "DLS_BLOCKED", "ARCHIVE_UNAVAILABLE"}:
                reason_counts[reason] += 1
                remaining.append(
                    {"record_ref_sha256": _record_ref(item), "state": state, "reason": reason}
                )
            if reason.startswith("unsupported_format:"):
                unsupported_formats[str(item.get("format") or "UNKNOWN")] += 1
            profile = _profile(item)
            if profile is None:
                continue
            profile_count += 1
            format_name = str(item.get("format") or "UNKNOWN")
            format_counts[format_name] += 1
            observations += len(_observations(profile))
            flags = _coverage_flags(profile)
            for field in REPORT_FIELDS:
                coverage[format_name][field] += int(flags[field])
            for conflict, present in _conflict_flags(profile).items():
                conflicts[conflict] += int(present)
    coverage_table = {
        format_name: {
            "profiles": format_counts[format_name],
            **{field: int(values[field]) for field in REPORT_FIELDS},
        }
        for format_name, values in sorted(coverage.items())
    }
    return {
        "schema": "openrag.document-metadata-full-backfill-evidence",
        "version": 1,
        "batch_checkpoints": len(batch_paths),
        "states": dict(sorted(state_counts.items())),
        "failure_reasons": dict(sorted(reason_counts.items())),
        "profiles_summarized": profile_count,
        "observations_summarized": observations,
        "format_counts": dict(sorted(format_counts.items())),
        "coverage_by_format": coverage_table,
        "conflict_analysis": dict(sorted(conflicts.items())),
        "unsupported_formats": dict(sorted(unsupported_formats.items())),
        "remaining_records": sorted(
            remaining, key=lambda value: (value["state"], value["record_ref_sha256"])
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _atomic_private_json(args.output, summarize(args.checkpoint_directory))


if __name__ == "__main__":
    main()
