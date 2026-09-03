from __future__ import annotations

from typing import Any

import pytest

from services.metadata_filter_side_index import MetadataFilterSideIndex


class _NotFound(Exception):
    status_code = 404


class _Indices:
    def __init__(self) -> None:
        self.indices: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, set[str]] = {}
        self.alias_transactions: list[list[dict[str, Any]]] = []

    async def exists(self, *, index: str) -> bool:
        return index in self.indices

    async def create(self, *, index: str, body: dict[str, Any]) -> None:
        self.indices[index] = body

    async def get_mapping(self, *, index: str) -> dict[str, Any]:
        return {index: {"mappings": self.indices[index]["mappings"]}}

    async def put_settings(self, *, index: str, body: dict[str, Any]) -> None:
        self.indices[index]["settings"]["index"].update(body["index"])

    async def refresh(self, *, index: str) -> None:
        assert index in self.indices

    async def get_alias(self, *, name: str) -> dict[str, Any]:
        if name not in self.aliases or not self.aliases[name]:
            raise _NotFound()
        return {index: {"aliases": {name: {}}} for index in self.aliases[name]}

    async def update_aliases(self, *, body: dict[str, Any]) -> None:
        actions = body["actions"]
        self.alias_transactions.append(actions)
        for action in actions:
            operation, value = next(iter(action.items()))
            targets = self.aliases.setdefault(value["alias"], set())
            if operation == "add":
                targets.add(value["index"])
            else:
                targets.discard(value["index"])


class _Client:
    def __init__(self) -> None:
        self.indices = _Indices()


@pytest.mark.asyncio
async def test_generation_mapping_and_first_generation_alias_rollback_are_atomic():
    client = _Client()
    generation = "documents-metadata-filter-v1-20260903t070000z"
    side_index = MetadataFilterSideIndex(client, index_name=generation)

    await side_index.create()

    assert await side_index.verify_mapping() is True
    # OpenSearch omits the redundant object type when properties already make
    # it unambiguous; that normalization must not weaken strict verification.
    client.indices.indices[generation]["mappings"]["properties"]["filter"].pop("type")
    assert await side_index.verify_mapping() is True
    assert client.indices.indices[generation]["settings"]["index"] == {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "refresh_interval": "-1",
    }
    old = await side_index.switch_alias()
    assert old == ()
    assert await side_index.current_alias_targets() == (generation,)
    assert len(client.indices.alias_transactions[-1]) == 1

    await side_index.restore_alias(old)
    assert await side_index.current_alias_targets() == ()
    await side_index.switch_alias()
    assert await side_index.current_alias_targets() == (generation,)


def test_generation_name_is_fail_closed():
    with pytest.raises(ValueError, match="namespace"):
        MetadataFilterSideIndex(_Client(), index_name="documents")
