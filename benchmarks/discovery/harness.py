"""CLI for discovery benchmark evaluation and review artifacts.

The live collection surface is intentionally read-only. It accepts captured
API JSON or a DLS files endpoint; it never changes workspace settings, indices,
embeddings, ranking, GitOps, or deployments.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from benchmarks.discovery.corpus import snapshot_visible_corpus
from benchmarks.discovery.ground_truth import load_ground_truth
from benchmarks.discovery.metrics import compute_metrics, coverage_success_rate
from benchmarks.discovery.review import build_review_rows, write_review


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value.get("data", value) if set(value) == {"status", "data"} else value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _annotate_variant_ranks(
    results: list[dict[str, Any]], retrieval_variant: str
) -> list[dict[str, Any]]:
    rank_field = {
        "lexical": "lexical_rank",
        "dense": "dense_rank",
        "rrf": "rrf_rank",
    }[retrieval_variant]
    score_field = {
        "lexical": "lexical_score",
        "dense": "dense_score",
        "rrf": "rrf_score",
    }[retrieval_variant]
    return [
        {
            **item,
            rank_field: rank,
            score_field: item.get(score_field, item.get("score")),
        }
        for rank, item in enumerate(results, start=1)
    ]


def _reproducibility_context(
    definition: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    baseline = metadata.get("baseline", {})
    retrieval = metadata.get("retrieval", {})
    corpus = metadata.get("corpus", {})
    return {
        "benchmark_id": definition["benchmark_id"],
        "benchmark_version": definition["benchmark_version"],
        "baseline_git_sha": baseline.get("source_sha"),
        "scope_policy_id": baseline.get("scope_policy_id"),
        "scope_policy_version": baseline.get("scope_policy_version"),
        "embedding_provider": retrieval.get("embedding_provider"),
        "embedding_model": retrieval.get("embedding_model"),
        "embedding_dimensions": retrieval.get("embedding_dimensions"),
        "embedding_index_version": retrieval.get("embedding_index_version"),
        "retrieval_settings": retrieval,
        "timestamp": corpus.get("benchmark_finished_at"),
        "visible_corpus_counts": {
            "before": corpus.get("before"),
            "after": corpus.get("after"),
            "changed_during_run": corpus.get("changed_during_run"),
        },
    }


def evaluate_capture(
    definition: dict[str, Any],
    focused: dict[str, Any],
    scope: dict[str, Any],
    *,
    k_values: list[int],
    exact_scope_k: int = 100,
    candidate_horizon: int = 100,
    retrieval_variant: str = "rrf",
    query_id: str | None = None,
    query_executed: str | None = None,
) -> list[dict[str, Any]]:
    selected_query = next(
        (
            query
            for query in definition["queries"]
            if query_id is None or query["query_id"] == query_id
        ),
        None,
    )
    if selected_query is None:
        raise ValueError(f"unknown query_id: {query_id}")
    seeds = _annotate_variant_ranks(
        [item for item in focused.get("results", []) if isinstance(item, dict)],
        retrieval_variant,
    )
    runs = []
    for k in k_values:
        has_exact_scope = k == exact_scope_k
        metrics = compute_metrics(
            definition,
            seeds,
            k=k,
            closure_documents=scope.get("documents") if has_exact_scope else None,
            coverage=scope.get("coverage") if has_exact_scope else None,
        )
        metrics.update(
            {
                "retrieval_variant": retrieval_variant,
                "query_id": selected_query["query_id"],
                "query_literal": selected_query["text"],
                "query_actually_executed": query_executed or selected_query["text"],
                "scope_seed_set_exact": has_exact_scope,
                "candidate_horizon": candidate_horizon,
                "candidate_horizon_supported": k <= candidate_horizon,
            }
        )
        runs.append(metrics)
    return runs


def _write_summary_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    fields = (
        "retrieval_variant",
        "requested_k",
        "available_seed_chunks",
        "effective_seed_chunks",
        "seed_occurrences",
        "seed_document_recall",
        "seed_component_recall",
        "post_prov_o_document_recall",
        "post_prov_o_component_recall",
        "precision",
        "expansion_factor_per_seed_occurrence",
        "coverage_complete",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for run in runs:
            row = dict(run)
            for field in (
                "seed_document_recall",
                "seed_component_recall",
                "post_prov_o_document_recall",
                "post_prov_o_component_recall",
                "precision",
            ):
                metric = row[field]
                row[field] = (
                    metric.get("value")
                    if metric.get("status") == "measured"
                    else metric.get("status")
                )
            writer.writerow({field: row.get(field) for field in fields})


def _review_command(args: argparse.Namespace) -> None:
    focused = _read_json(args.focused)
    scope = _read_json(args.scope)
    rows = build_review_rows(focused, scope)
    write_review(rows, args.output_json, args.output_csv)
    print(
        json.dumps(
            {"candidates": len(rows), "json": str(args.output_json), "csv": str(args.output_csv)}
        )
    )


def _evaluate_command(args: argparse.Namespace) -> None:
    definition = load_ground_truth(args.definition)
    focused = _read_json(args.focused)
    scope = _read_json(args.scope)
    metadata = _read_json(args.metadata) if args.metadata else {}
    runs = evaluate_capture(
        definition,
        focused,
        scope,
        k_values=args.k,
        exact_scope_k=args.exact_scope_k,
        candidate_horizon=args.candidate_horizon,
        retrieval_variant=args.retrieval_variant,
        query_id=args.query_id,
        query_executed=args.query_executed,
    )
    context = _reproducibility_context(definition, metadata)
    for run in runs:
        run.update(context)
    result = {
        "schema_version": 1,
        "benchmark_id": definition["benchmark_id"],
        "benchmark_version": definition["benchmark_version"],
        "document_metric_unit": definition["document_metric_unit"],
        "metadata": metadata,
        "runs": runs,
        "coverage_success_rate": coverage_success_rate(runs),
        "limitations": [
            "The public API exposes the fused RRF result but not lexical/dense lane ranks or scores.",
            "The deployed scope_exhaustive seed budget is fixed at 100; other post-PROV-O K values are not inferred.",
            "K counts ranked chunks; document metrics de-duplicate by source occurrence.",
        ],
    }
    _write_json(args.output_json, result)
    _write_summary_csv(args.output_csv, runs)


def _corpus_command(args: argparse.Namespace) -> None:
    _write_json(args.output, snapshot_visible_corpus(args.base_url, page_size=args.page_size))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="build human-review candidate JSON/CSV")
    review.add_argument("--focused", type=Path, required=True)
    review.add_argument("--scope", type=Path, required=True)
    review.add_argument("--output-json", type=Path, required=True)
    review.add_argument("--output-csv", type=Path, required=True)
    review.set_defaults(func=_review_command)

    evaluate = commands.add_parser("evaluate", help="compute metrics from captured API JSON")
    evaluate.add_argument("--definition", type=Path, required=True)
    evaluate.add_argument("--focused", type=Path, required=True)
    evaluate.add_argument("--scope", type=Path, required=True)
    evaluate.add_argument("--output-json", type=Path, required=True)
    evaluate.add_argument("--output-csv", type=Path, required=True)
    evaluate.add_argument("--metadata", type=Path)
    evaluate.add_argument("--k", type=int, action="append", default=[])
    evaluate.add_argument("--exact-scope-k", type=int, default=100)
    evaluate.add_argument("--candidate-horizon", type=int, default=100)
    evaluate.add_argument(
        "--retrieval-variant",
        choices=("lexical", "dense", "rrf"),
        default="rrf",
    )
    evaluate.add_argument("--query-id")
    evaluate.add_argument("--query-executed")
    evaluate.set_defaults(func=_evaluate_command)

    corpus = commands.add_parser("corpus", help="snapshot the DLS-visible corpus")
    corpus.add_argument("--base-url", required=True)
    corpus.add_argument("--page-size", type=int, default=1000)
    corpus.add_argument("--output", type=Path, required=True)
    corpus.set_defaults(func=_corpus_command)
    return root


def main() -> None:
    args = parser().parse_args()
    if hasattr(args, "k") and not args.k:
        args.k = [20, 50, 100, 200]
    args.func(args)


if __name__ == "__main__":
    main()
