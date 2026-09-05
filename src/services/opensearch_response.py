"""Execution facts shared by correctness-relevant OpenSearch searches.

OpenSearch Search API: skipped shards are included in successful, and optional
terminated_early is present only for applicable requests. Exact totals/cursor
checks apply to exhaustive pagination, not ranked/ANN membership.
https://docs.opensearch.org/latest/api-reference/search-apis/search/
"""

from __future__ import annotations

from typing import Any


def validate_search_response(response: dict[str, Any], *, exact_total: bool = False) -> list[str]:
    """Return explicit failures; absence of execution evidence fails closed."""
    failures: list[str] = []
    if response.get("timed_out") is not False:
        failures.append("opensearch_timed_out_or_unknown")
    if "terminated_early" in response and response["terminated_early"] is not False:
        failures.append("opensearch_terminated_early_or_unknown")
    shards = response.get("_shards")
    if not isinstance(shards, dict) or any(
        type(shards.get(key)) is not int or shards[key] < 0
        for key in ("total", "successful", "failed")
    ):
        failures.append("opensearch_shard_execution_unknown")
    else:
        if shards["failed"] or shards["successful"] != shards["total"]:
            failures.append("opensearch_shard_execution_incomplete")
        skipped = shards.get("skipped", 0)
        if type(skipped) is not int or not 0 <= skipped <= shards["successful"]:
            failures.append("opensearch_shard_counters_invalid")
        if shards.get("failures"):
            failures.append("opensearch_shard_failures")
    if response.get("failures"):
        failures.append("opensearch_failures")
    hits = response.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        failures.append("opensearch_hits_invalid")
    elif exact_total:
        total = hits.get("total")
        if isinstance(total, dict):
            if total.get("relation") != "eq":
                failures.append("opensearch_hit_total_inexact")
            total = total.get("value")
        if type(total) is not int or total < 0:
            failures.append("opensearch_hit_total_invalid")
    return failures


def validate_search_progress(previous: list[Any] | None, current: Any, *, width: int) -> None:
    """Require an ascending, total search_after key for the declared sort."""
    if not isinstance(current, list) or len(current) != width or any(v is None for v in current):
        raise ValueError("OpenSearch pagination has an invalid cursor")
    if previous is not None:
        try:
            advanced = tuple(current) > tuple(previous)
        except TypeError as exc:
            raise ValueError("OpenSearch pagination cursor types changed") from exc
        if not advanced:
            raise ValueError("OpenSearch pagination cursor did not advance")
