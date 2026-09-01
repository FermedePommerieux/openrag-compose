"""Drive read-only retrieval correctness probes in the deployed backend."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MARKER = "RETRIEVAL_CORRECTNESS_JSON="


def _parse_remote_output(output: str) -> dict[str, Any]:
    offset = output.rfind(_MARKER)
    if offset < 0:
        raise ValueError("remote correctness result marker is missing")
    value = json.loads(output[offset + len(_MARKER) :].strip())
    if not isinstance(value, dict):
        raise ValueError("remote correctness result must be an object")
    return value


def _capture(args: argparse.Namespace) -> None:
    plan = json.loads(args.plan_json)
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    script_b64 = base64.b64encode(args.remote_script.read_bytes()).decode("ascii")
    plan_b64 = base64.b64encode(json.dumps(plan, ensure_ascii=False).encode()).decode("ascii")
    bootstrap = (
        "import base64,sys;script=sys.argv[1];plan=sys.argv[2];"
        "sys.argv=['remote_retrieval_correctness.py',plan];"
        "exec(base64.b64decode(script))"
    )
    remote_args = [
        "sudo",
        "k3s",
        "kubectl",
        "exec",
        "-n",
        args.namespace,
        f"deployment/{args.deployment}",
        "--",
        "env",
        "PYTHONPATH=/app/src",
        "python",
        "-c",
        bootstrap,
        script_b64,
        plan_b64,
    ]
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", args.ssh_host, shlex.join(remote_args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    if completed.returncode:
        raise RuntimeError(f"remote correctness probe failed: {completed.stderr[-12000:]}")
    result = _parse_remote_output(completed.stdout)
    capture = {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "benchmark_driver_sha256": __import__("hashlib")
        .sha256(Path(__file__).read_bytes())
        .hexdigest(),
        "remote_script_sha256": __import__("hashlib")
        .sha256(args.remote_script.read_bytes())
        .hexdigest(),
        "evidence_context": plan.get("evidence_context", {}),
        "plan": plan,
        "result": result,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(capture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture",))
    parser.add_argument("--remote-script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--namespace", default="openrag")
    parser.add_argument("--deployment", default="openrag-backend")
    parser.add_argument("--timeout", type=int, default=600)
    return parser


def main() -> None:
    args = _parser().parse_args()
    _capture(args)


if __name__ == "__main__":
    main()
