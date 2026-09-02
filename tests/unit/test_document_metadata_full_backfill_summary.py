"""Sanitization and aggregation contract for full-backfill evidence."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_summary() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "summarize_document_metadata_full_backfill.py"
    spec = importlib.util.spec_from_file_location("metadata_full_backfill_summary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUMMARY = _load_summary()


def _observation(
    field: str,
    value: str,
    *,
    section: str = "embedded",
    source: str = "ooxml_core_properties",
    status: str = "normalized",
    timezone: str | None = None,
) -> dict[str, object]:
    return {
        "field": field,
        "value": value,
        "section": section,
        "source": source,
        "normalization_status": status,
        "timezone": timezone,
    }


def _profile() -> dict[str, object]:
    return {
        "identity": [],
        "embedded": [
            _observation("embedded_created_at", "2025-01-01T00:00:00", timezone="UNKNOWN"),
            _observation("creator", "Alice"),
            _observation("last_modified_by", "Bob"),
            _observation("producer", "PDF producer", source="pdf_info"),
            _observation("producer", "XMP producer", source="pdf_xmp"),
        ],
        "filesystem": [],
        "archive": [
            _observation(
                "archive_object_id",
                "private-object",
                section="archive",
                source="archive_registry",
            )
        ],
        "ingestion": [],
        "conflicts": [
            {
                "field": "producer",
                "values": ["PDF producer", "XMP producer"],
                "sources": ["pdf_info", "pdf_xmp"],
            }
        ],
    }


def test_summary_aggregates_fields_and_hashes_remaining_identity(tmp_path: Path):
    batches = tmp_path / "batches"
    batches.mkdir()
    profile = _profile()
    payload = {
        "items": {
            "storage-private-1": {
                "state": "VERIFIED",
                "changed": True,
                "format": "PDF",
                "record": {
                    "storage_id": "storage-private-1",
                    "document_id": "document-private-1",
                    "source_entity_id": "entity-private-1",
                },
                "expected_metadata": {
                    "present": {"document_metadata_profile": profile},
                },
            },
            "storage-private-2": {
                "state": "VERIFIED",
                "changed": False,
                "format": "PDF",
                "record": {
                    "storage_id": "storage-private-2",
                    "document_id": "document-private-2",
                    "source_entity_id": "entity-private-2",
                },
                "pre_metadata": {"present": {"document_metadata_profile": profile}},
            },
            "storage-private-3": {
                "state": "ARCHIVE_UNAVAILABLE",
                "reason": "archive_registry_status_failed",
                "record": {
                    "storage_id": "storage-private-3",
                    "document_id": "document-private-3",
                    "source_entity_id": "entity-private-3",
                },
            },
        }
    }
    (batches / "batch-00000.json").write_text(json.dumps(payload))

    result = SUMMARY.summarize(tmp_path)

    assert result["states"] == {
        "ARCHIVE_UNAVAILABLE": 1,
        "UNCHANGED": 1,
        "VERIFIED": 1,
    }
    assert result["coverage_by_format"]["PDF"]["profiles"] == 2
    assert result["coverage_by_format"]["PDF"]["embedded_created_at"] == 2
    assert result["coverage_by_format"]["PDF"]["archive_metadata"] == 2
    assert result["conflict_analysis"]["pdf_info_vs_xmp"] == 2
    assert result["conflict_analysis"]["creator_vs_last_modified_by"] == 2
    assert result["conflict_analysis"]["missing_timezone"] == 2
    remaining = result["remaining_records"]
    assert len(remaining) == 1
    assert len(remaining[0]["record_ref_sha256"]) == 64
    encoded = json.dumps(result)
    assert "storage-private" not in encoded
    assert "document-private" not in encoded
    assert "entity-private" not in encoded

