"""Orchestrate the bounded read-only post-backfill production audit."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import shlex
import subprocess
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULT_MARKER = "POST_BACKFILL_ASSOCIATION_AUDIT_JSON="
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks/document-metadata/post-backfill-association-audit.json"
DEFAULT_REVIEW = ROOT / "benchmarks/document-metadata/association-neighborhood-human-review.csv"
MODULE_PATHS = {
    "models.document_investigation": ROOT / "src/models/document_investigation.py",
    "models.metadata_filter": ROOT / "src/models/metadata_filter.py",
    "services.document_investigation": ROOT / "src/services/document_investigation.py",
    "services.metadata_filter": ROOT / "src/services/metadata_filter.py",
}
REMOTE_PATH = ROOT / "benchmarks/remote_post_backfill_association_audit.py"
REVIEW_FIELDS = (
    "seed_document_id",
    "neighbor_document_id",
    "seed_safe_name",
    "neighbor_safe_name",
    "source_system",
    "format_type",
    "production_month_observations",
    "production_year_observations",
    "association_strength",
    "association_dimensions",
    "short_explanation",
    "human_judgment",
    "human_note",
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=REVIEW_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            if row.get("human_judgment") or row.get("human_note"):
                raise ValueError("human review labels and notes must remain blank")
            writer.writerow(row)


def _remote_bootstrap(plan: dict[str, Any]) -> str:
    payload = {
        "plan": plan,
        "modules": {
            module_name: path.read_text(encoding="utf-8")
            for module_name, path in MODULE_PATHS.items()
        },
        "remote": REMOTE_PATH.read_text(encoding="utf-8"),
    }
    encoded = base64.b64encode(
        zlib.compress(json.dumps(payload, ensure_ascii=False).encode(), level=9)
    ).decode("ascii")
    return f"""\
import base64,json,sys,types,zlib
payload=json.loads(zlib.decompress(base64.b64decode({encoded!r})))
for name in ({tuple(MODULE_PATHS)!r}):
    module=types.ModuleType(name)
    module.__file__='<in-memory:'+name+'>'
    module.__package__=name.rpartition('.')[0]
    sys.modules[name]=module
    exec(compile(payload['modules'][name],module.__file__,'exec'),module.__dict__)
namespace={{'__name__':'benchmarks.remote_post_backfill_association_audit',
           '__package__':'benchmarks',
           '__file__':'<in-memory:remote-audit>'}}
exec(compile(payload['remote'],namespace['__file__'],'exec'),namespace)
namespace['remote_entry'](payload['plan'])
"""


def _git_value(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def capture(args: argparse.Namespace) -> None:
    plan = {
        "schema": "openrag.post-backfill-association-audit-plan",
        "version": 1,
        "index": args.index,
        "batch_size": args.batch_size,
        "bucket_member_limit": args.bucket_member_limit,
        "pair_limit_per_bucket": args.pair_limit_per_bucket,
        "global_pair_limit": args.global_pair_limit,
        "cohort_size": args.cohort_size,
        "expected_documents": 47_400,
        "expected_occurrences": 47_454,
        "expected_profile_documents": 47_132,
        "expected_profile_occurrences": 47_133,
        "expected_unprofiled": 268,
        "historical_successful_profiles": 47_130,
        "historical_non_enriched_records": 270,
        "extraction_impossible": 232,
        "archive_source_unavailable": 38,
        "expected_corpus_digest": (
            "038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7"
        ),
        "production_writes": 0,
    }
    remote_args = [
        "sudo",
        "kubectl",
        "-n",
        args.namespace,
        "exec",
        "-i",
        f"deploy/{args.deployment}",
        "--",
        "env",
        "PYTHONPATH=/app/src",
        "python",
        "-",
    ]
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        "-i",
        str(args.ssh_key),
        args.ssh_host,
        shlex.join(remote_args),
    ]
    completed = subprocess.run(
        command,
        input=_remote_bootstrap(plan),
        check=False,
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    if completed.returncode:
        raise RuntimeError(
            "remote read-only audit failed:\n" + completed.stderr[-12_000:]
        )
    marker_offset = completed.stdout.rfind(RESULT_MARKER)
    if marker_offset < 0:
        raise RuntimeError("remote audit marker missing:\n" + completed.stdout[-12_000:])
    result = json.loads(completed.stdout[marker_offset + len(RESULT_MARKER) :].strip())
    result["capture"] = {
        "captured_locally_at": datetime.now(UTC).isoformat(),
        "repository": "FermedePommerieux/openrag-compose",
        "target_branch": "pommerieux/v0.6.0-retrieval-v2-prov-o",
        "work_branch": _git_value("branch", "--show-current"),
        "head": _git_value("rev-parse", "HEAD"),
        "worktree": str(ROOT),
        "ssh_host": args.ssh_host,
        "namespace": args.namespace,
        "deployment": args.deployment,
    }
    review_rows = list(result.pop("human_review_rows"))
    result["human_review_artifact"] = {
        "path": str(args.review_output),
        "rows": len(review_rows),
        "judgments_blank": all(not item.get("human_judgment") for item in review_rows),
        "notes_blank": all(not item.get("human_note") for item in review_rows),
        "labels": ["USEFUL", "MARGINAL", "NOT_USEFUL"],
        "qrels": False,
    }
    _write_review_csv(args.review_output, review_rows)
    _write_json(args.output, result)
    print(json.dumps({"output": str(args.output), **result["human_review_artifact"]}, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounded read-only post-backfill association audit"
    )
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--namespace", default="openrag")
    parser.add_argument("--deployment", default="openrag-backend")
    parser.add_argument("--index", default="documents")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--bucket-member-limit", type=int, default=51)
    parser.add_argument("--pair-limit-per-bucket", type=int, default=25)
    parser.add_argument("--global-pair-limit", type=int, default=20_000)
    parser.add_argument("--cohort-size", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--review-output", type=Path, default=DEFAULT_REVIEW)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size must be between 1 and 1000")
    if args.bucket_member_limit < 26:
        parser.error("--bucket-member-limit must be at least 26")
    if args.cohort_size < 20:
        parser.error("--cohort-size must be at least 20")
    return args


if __name__ == "__main__":
    try:
        capture(parse_args())
    except Exception as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        raise
