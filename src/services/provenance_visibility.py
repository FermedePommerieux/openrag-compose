"""Backend-only existence classification for DLS-invisible relation targets.

Never retrieves hidden source fields or returns an externally callable oracle.
Only targets already asserted by a validated visible source may be submitted.
No target identifiers or hidden counts belong in public coverage diagnostics.
"""

from __future__ import annotations

from typing import Any

from services.opensearch_response import validate_search_response

BATCH_SIZE = 100
MAX_TARGETS = 1000


async def resolve_dls_hidden_targets(
    reader: Any, control: Any, *, index: str, targets: list[str]
) -> set[str]:
    if control is None:
        return set()  # Unproven absence remains unresolved.
    identifiers = sorted(set(targets))
    if len(identifiers) > MAX_TARGETS:
        raise ValueError("Provenance visibility classification limit reached")
    hidden: set[str] = set()
    for offset in range(0, len(identifiers), BATCH_SIZE):
        batch = identifiers[offset : offset + BATCH_SIZE]
        filters = {
            str(i): {
                "bool": {
                    "should": [
                        {"term": {"source_entity_id": target}},
                        {"term": {"source_entity_alternate_ids": target}},
                    ],
                    "minimum_should_match": 1,
                }
            }
            for i, target in enumerate(batch)
        }
        body = {
            "size": 0,
            "_source": False,
            "track_total_hits": True,
            "query": {"bool": {"should": list(filters.values()), "minimum_should_match": 1}},
            "aggs": {"targets": {"filters": {"filters": filters}}},
        }

        async def observe(client, body=body, filters=filters, batch=batch):
            result = await client.search(index=index, body=body, params={"terminate_after": 0})
            if validate_search_response(result, exact_total=True) or result["hits"]["hits"]:
                raise ValueError("Provenance visibility classification incomplete")
            buckets = result.get("aggregations", {}).get("targets", {}).get("buckets", {})
            if set(buckets) != set(filters):
                raise ValueError("Provenance visibility classification buckets missing")
            counts = [buckets[str(i)].get("doc_count") for i in range(len(batch))]
            if any(type(count) is not int or count < 0 for count in counts):
                raise ValueError("Provenance visibility classification counts invalid")
            return counts

        # Use the unfiltered reader view: an eligible target omitted by an
        # explicit search filter or broken chunk-zero representative is not
        # evidence of a DLS boundary and must still fail completeness.
        reader_before = await observe(reader)
        control_before = await observe(control)
        reader_after = await observe(reader)
        control_after = await observe(control)
        if reader_before != reader_after or control_before != control_after:
            raise ValueError("Provenance visibility classification changed during observation")
        if any(r > c for r, c in zip(reader_after, control_after, strict=True)):
            raise ValueError("Provenance visibility classification views inconsistent")
        hidden.update(
            target
            for target, r, c in zip(batch, reader_after, control_after, strict=True)
            if r == 0 and c > 0
        )
    return hidden
