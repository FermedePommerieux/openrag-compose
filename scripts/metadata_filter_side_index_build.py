#!/usr/bin/env python3
"""Build, verify, and optionally activate one production metadata side-index generation."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULT_MARKER = "METADATA_FILTER_SIDE_INDEX_BUILD_JSON="
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks/document-metadata/metadata-filter-side-index-build.json"
REMOTE_PATH = ROOT / "benchmarks/remote_metadata_filter_side_index_build.py"
MODULE_PATHS = {
    "models.document_metadata": ROOT / "src/models/document_metadata.py",
    "models.source_provenance": ROOT / "src/models/source_provenance.py",
    "models.document_investigation": ROOT / "src/models/document_investigation.py",
    "models.metadata_filter": ROOT / "src/models/metadata_filter.py",
    "models.metadata_filter_projection": ROOT / "src/models/metadata_filter_projection.py",
    "models.structured_document_query": ROOT / "src/models/structured_document_query.py",
    "services.document_investigation": ROOT / "src/services/document_investigation.py",
    "services.metadata_filter": ROOT / "src/services/metadata_filter.py",
    "services.metadata_filter_projection": ROOT / "src/services/metadata_filter_projection.py",
    "services.metadata_candidate_restriction": (
        ROOT / "src/services/metadata_candidate_restriction.py"
    ),
    "services.metadata_filter_side_index": ROOT / "src/services/metadata_filter_side_index.py",
}


def _git_value(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


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
    order = tuple(MODULE_PATHS)
    return f"""\
import base64,json,sys,types,zlib
payload=json.loads(zlib.decompress(base64.b64decode({encoded!r})))
for name in ({order!r}):
    module=types.ModuleType(name)
    module.__file__='<in-memory:'+name+'>'
    module.__package__=name.rpartition('.')[0]
    sys.modules[name]=module
    exec(compile(payload['modules'][name],module.__file__,'exec'),module.__dict__)
namespace={{'__name__':'benchmarks.remote_metadata_filter_side_index_build',
           '__package__':'benchmarks',
           '__file__':'<in-memory:remote-metadata-filter-side-index-build>'}}
exec(compile(payload['remote'],namespace['__file__'],'exec'),namespace)
namespace['remote_entry'](payload['plan'])
"""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def capture(args: argparse.Namespace) -> None:
    plan = {
        "schema": "openrag.metadata-filter-side-index-build-plan",
        "version": 1,
        "source_index": args.index,
        "generation": args.generation,
        "batch_size": args.batch_size,
        "expected_corpus_digest": args.expected_corpus_digest,
        "activate_alias": args.activate_alias,
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
        raise RuntimeError("remote side-index build failed:\n" + completed.stderr[-20_000:])
    marker_offset = completed.stdout.rfind(RESULT_MARKER)
    if marker_offset < 0:
        raise RuntimeError("remote side-index result marker missing:\n" + completed.stdout[-20_000:])
    result = json.loads(completed.stdout[marker_offset + len(RESULT_MARKER) :].strip())
    result["capture"] = {
        "captured_locally_at": datetime.now(UTC).isoformat(),
        "repository": "FermedePommerieux/openrag-compose",
        "target_branch": "pommerieux/v0.6.0-retrieval-v2-prov-o",
        "work_branch": _git_value("branch", "--show-current"),
        "starting_sha": "54bd1013523ea7dc7b026abd1c82bb8ca79066d7",
        "head": _git_value("rev-parse", "HEAD"),
        "worktree": str(ROOT),
        "ssh_host": args.ssh_host,
        "namespace": args.namespace,
        "deployment": args.deployment,
    }
    _write_json(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "generation": result["generation"],
                "projected": result["projection"]["projected"],
                "failed": result["projection"]["failed"],
                "dls": result["dls"]["count_aggregation_scoped"],
                "alias": result["alias_cutover"],
                "rollback": result["rollback"],
            },
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--namespace", default="openrag")
    parser.add_argument("--deployment", default="openrag-backend")
    parser.add_argument("--index", default="documents")
    parser.add_argument("--generation", required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--expected-corpus-digest",
        default="038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7",
    )
    parser.add_argument("--activate-alias", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size must be between 1 and 1000")
    return args


if __name__ == "__main__":
    try:
        capture(parse_args())
    except Exception as exc:
        print(f"side-index build failed: {exc}", file=sys.stderr)
        raise
