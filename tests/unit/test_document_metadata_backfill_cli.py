"""Checkpoint/resume contract for the metadata-only backfill CLI."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_cli() -> ModuleType:
    scripts = Path(__file__).parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "document_metadata_backfill_cli",
            scripts / "document_metadata_backfill.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


CLI = _load_cli()


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values = {
        "audit_only": False,
        "write": False,
        "index": "openrag-test",
        "batch_size": 25,
        "concurrency": 1,
        "result_log": str(tmp_path / "results.jsonl"),
        "checkpoint": str(tmp_path / "checkpoint.json"),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_checkpoint_round_trip_restores_cursor_and_bounded_state(tmp_path: Path):
    args = _args(tmp_path)
    state = CLI._empty_state()
    CLI._accumulate(
        state,
        [
            {
                "status": "SUCCESS",
                "reason": "dry_run_would_update",
                "chunks_updated": 0,
                "bytes_read": 42,
                "conflicts": 1,
                "extraction_ms": 2.5,
            }
        ],
    )
    cursor = ["attachment", "mail", "doc", "chunk"]

    CLI._checkpoint(args, "2026-09-02T00:00:00+00:00", state, cursor, 17)
    payload = json.loads(Path(args.checkpoint).read_text())
    restored = CLI._restore_checkpoint(
        args,
        Path(args.checkpoint),
        "ignored",
    )

    assert "results" not in payload
    assert restored == ("2026-09-02T00:00:00+00:00", cursor, 17, state)
    assert restored[3]["would_update"] == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"index": "different-index"}, "checkpoint index differs"),
        ({"write": True}, "checkpoint mode differs"),
    ],
)
def test_resume_rejects_a_different_index_or_mode(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
):
    original = _args(tmp_path)
    state = CLI._empty_state()
    CLI._checkpoint(original, "start", state, ["cursor"], 1)

    with pytest.raises(ValueError, match=message):
        CLI._restore_checkpoint(
            _args(tmp_path, **override),
            Path(original.checkpoint),
            "ignored",
        )


def test_result_log_is_append_only_and_document_scoped(tmp_path: Path):
    path = tmp_path / "results.jsonl"
    first = [{"document_id": "doc-1", "status": "SUCCESS"}]
    second = [{"document_id": "doc-2", "status": "UNCHANGED"}]

    CLI._append_result_log(path, first)
    CLI._append_result_log(path, second)

    assert [json.loads(line) for line in path.read_text().splitlines()] == first + second
