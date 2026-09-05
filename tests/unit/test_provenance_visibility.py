"""Only proven DLS exclusion can close an otherwise opaque provenance branch."""

from copy import deepcopy

import pytest

from services.provenance_visibility import resolve_dls_hidden_targets


class CountClient:
    def __init__(self, counts, *, broken=False, changing=False):
        self.counts = counts
        self.broken = broken
        self.changing = changing
        self.requests = []

    async def search(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        body = kwargs["body"]
        assert body["size"] == 0 and body["_source"] is False
        filters = body["aggs"]["targets"]["filters"]["filters"]
        buckets = {
            key: {
                "doc_count": self.counts.get(
                    value["bool"]["should"][0]["term"]["source_entity_id"], 0
                )
            }
            for key, value in filters.items()
        }
        if self.changing and len(self.requests) % 2 == 0:
            buckets[next(iter(buckets))]["doc_count"] += 1
        return {
            "timed_out": self.broken,
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {
                "hits": [],
                "total": {"value": sum(v["doc_count"] for v in buckets.values()), "relation": "eq"},
            },
            "aggregations": {"targets": {"buckets": buckets}},
        }


@pytest.mark.asyncio
async def test_hidden_missing_and_visible_but_graph_omitted_are_distinct():
    reader = CountClient({"visible-broken-representative": 2})
    control = CountClient({"hidden": 3, "visible-broken-representative": 2})
    result = await resolve_dls_hidden_targets(
        reader,
        control,
        index="documents",
        targets=["hidden", "missing", "visible-broken-representative"],
    )
    assert result == {"hidden"}
    assert len(reader.requests) == len(control.requests) == 2
    assert all("chunk_index" not in str(q["body"]) for q in reader.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["broken", "changing"])
async def test_incomplete_or_unstable_control_view_never_proves_exclusion(fault):
    with pytest.raises(ValueError, match="classification"):
        await resolve_dls_hidden_targets(
            CountClient({}),
            CountClient({"hidden": 1}, **{fault: True}),
            index="documents",
            targets=["hidden"],
        )


@pytest.mark.asyncio
async def test_classifier_is_batched_and_bounded():
    targets = [f"target-{i}" for i in range(205)]
    reader, control = CountClient({}), CountClient(dict.fromkeys(targets, 1))
    assert await resolve_dls_hidden_targets(
        reader, control, index="documents", targets=targets
    ) == set(targets)
    assert len(reader.requests) == 6
    assert all(
        len(q["body"]["aggs"]["targets"]["filters"]["filters"]) <= 100 for q in control.requests
    )
    with pytest.raises(ValueError, match="limit"):
        await resolve_dls_hidden_targets(
            reader, control, index="documents", targets=[str(i) for i in range(1001)]
        )
