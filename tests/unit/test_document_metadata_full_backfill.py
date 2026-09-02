"""Fail-closed resume contracts for the full metadata backfill runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_runner() -> ModuleType:
    scripts = Path(__file__).parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "document_metadata_full_backfill_runner",
            scripts / "document_metadata_full_backfill.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


RUNNER = _load_runner()


def _identity(existing_profiles: int = 99, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "openrag.document-metadata-full-backfill-run",
        "version": 1,
        "index": "documents",
        "documents": 47_400,
        "eligible": 47_362,
        "archive_unavailable": 38,
        "ambiguous": 0,
        "existing_profiles": existing_profiles,
        "batch_size": 100,
        "cohort_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


def test_resume_preserves_initial_profile_count(tmp_path: Path):
    path = tmp_path / "run-identity.json"
    initial = _identity(existing_profiles=99)
    RUNNER._resolve_run_identity(path, initial)

    resumed = RUNNER._resolve_run_identity(path, _identity(existing_profiles=25_929))

    assert resumed == initial
    assert json.loads(path.read_text()) == initial


def test_resume_rejects_an_immutable_cohort_change(tmp_path: Path):
    path = tmp_path / "run-identity.json"
    RUNNER._resolve_run_identity(path, _identity())

    with pytest.raises(RuntimeError, match="run identity differs"):
        RUNNER._resolve_run_identity(path, _identity(documents=47_399))


def test_only_fully_terminal_checkpoints_are_skipped():
    terminal = SimpleNamespace(
        data={"items": {"one": {"state": "VERIFIED"}, "two": {"state": "FAILED"}}}
    )
    partial = SimpleNamespace(
        data={"items": {"one": {"state": "VERIFIED"}, "two": {"state": "WRITTEN"}}}
    )

    assert RUNNER._checkpoint_is_terminal(terminal) is True
    assert RUNNER._checkpoint_is_terminal(partial) is False


def test_incremental_aggregate_replaces_one_batch_without_rescanning():
    aggregate = {
        "processed": 25_850,
        "states": {"VERIFIED": 25_800, "ARCHIVE_UNAVAILABLE": 50},
        "bytes_read": 1_000,
        "opensearch_writes": 25_700,
        "formats": {"PDF": 4_000, "EML": 21_850},
        "failure_reasons": {"missing": 50},
        "timing_values": {},
    }
    before = {
        "processed": 50,
        "states": {"VERIFIED": 50},
        "bytes_read": 100,
        "opensearch_writes": 49,
        "formats": {"PDF": 50},
        "failure_reasons": {},
        "timing_values": {},
    }
    after = {
        "processed": 100,
        "states": {"VERIFIED": 99, "ARCHIVE_UNAVAILABLE": 1},
        "bytes_read": 300,
        "opensearch_writes": 98,
        "formats": {"PDF": 99},
        "failure_reasons": {"missing": 1},
        "timing_values": {},
    }

    RUNNER._apply_aggregate_delta(aggregate, before, after)

    assert aggregate["processed"] == 25_900
    assert aggregate["states"] == {"ARCHIVE_UNAVAILABLE": 51, "VERIFIED": 25_849}
    assert aggregate["bytes_read"] == 1_200
    assert aggregate["opensearch_writes"] == 25_749
    assert aggregate["formats"] == {"EML": 21_850, "PDF": 4_049}
    assert aggregate["failure_reasons"] == {"missing": 51}
