"""Evaluate deterministic natural-language metadata planning against corpus v1."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from services.metadata_query_planner import plan_metadata_query

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = ROOT / "benchmarks" / "metadata-planner" / "corpus-v1.json"


def _filter_key(payload: dict[str, Any]) -> str:
    pieces = [str(payload["field"]), str(payload["operator"])]
    value = payload.get("value")
    if isinstance(value, list):
        pieces.append("|".join(sorted(str(item) for item in value)))
    elif value is not None:
        pieces.append(str(value))
    if payload.get("calendar_basis"):
        pieces.append(str(payload["calendar_basis"]))
    return ":".join(pieces)


def evaluate(corpus: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    for case in corpus:
        started = time.perf_counter()
        plan = plan_metadata_query(case["query"])
        latencies_ms.append((time.perf_counter() - started) * 1000)
        actual_filters = sorted(_filter_key(item.canonical_payload()) for item in plan.filters)
        expected_filters = sorted(case["filters"])
        row = {
            "id": case["id"],
            "status_exact": plan.status.value == case["status"],
            "filters_exact": actual_filters == expected_filters,
            "free_text_exact": plan.free_text == case["free_text"],
            "actual_status": plan.status.value,
            "actual_filters": actual_filters,
            "actual_free_text": plan.free_text,
        }
        row["exact"] = row["status_exact"] and row["filters_exact"] and row["free_text_exact"]
        rows.append(row)

    count = len(rows)
    no_filter_cases = [
        (case, row) for case, row in zip(corpus, rows, strict=True) if not case["filters"]
    ]
    filter_cases = [
        (case, row) for case, row in zip(corpus, rows, strict=True) if case["filters"]
    ]
    unsupported = [
        row for case, row in zip(corpus, rows, strict=True) if case["status"] == "UNSUPPORTED"
    ]
    ambiguous = [
        row for case, row in zip(corpus, rows, strict=True) if case["status"] == "AMBIGUOUS"
    ]
    false_positive = sum(bool(row["actual_filters"]) for _case, row in no_filter_cases)
    false_negative = sum(not row["actual_filters"] for _case, row in filter_cases)
    ordered_latency = sorted(latencies_ms)
    p95_index = min(len(ordered_latency) - 1, int(len(ordered_latency) * 0.95))
    return {
        "schema": "openrag.metadata-planner-benchmark-result",
        "version": 1,
        "planner_mode": "DETERMINISTIC_ONLY",
        "cases": count,
        "exact_parse_accuracy": sum(row["exact"] for row in rows) / count,
        "false_positive_filter_rate": false_positive / max(1, len(no_filter_cases)),
        "false_negative_filter_rate": false_negative / max(1, len(filter_cases)),
        "unsupported_accuracy": sum(row["actual_status"] == "UNSUPPORTED" for row in unsupported)
        / max(1, len(unsupported)),
        "ambiguity_accuracy": sum(row["actual_status"] == "AMBIGUOUS" for row in ambiguous)
        / max(1, len(ambiguous)),
        "free_text_preservation": sum(row["free_text_exact"] for row in rows) / count,
        "planner_latency_ms": {
            "mean": statistics.fmean(latencies_ms),
            "p50": statistics.median(latencies_ms),
            "p95": ordered_latency[p95_index],
            "max": max(latencies_ms),
        },
        "llm_calls": 0,
        "additional_model_tokens": 0,
        "failures": [row for row in rows if not row["exact"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    result = evaluate(corpus)
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    raise SystemExit(0 if not result["failures"] else 1)


if __name__ == "__main__":
    main()
