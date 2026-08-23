from types import SimpleNamespace

import pytest

from utils.rrf_mapping import RRFMappingError, require_sortable_chunk_id_mapping


class _Client:
    def __init__(self, mapping):
        self.indices = SimpleNamespace(get_mapping=self._get_mapping)
        self.mapping = mapping

    async def _get_mapping(self, *, index):
        return {index: {"mappings": {"properties": self.mapping}}}


@pytest.mark.asyncio
async def test_rrf_mapping_accepts_keyword_and_legacy_missing_values():
    """Legacy documents may omit the value; the field mapping must be sortable."""
    await require_sortable_chunk_id_mapping(_Client({"chunk_id": {"type": "keyword"}}), "documents")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping, expected",
    [
        ({}, "not mapped"),
        ({"chunk_id": {"type": "text"}}, "must be mapped as keyword"),
        ({"chunk_id": {"type": "keyword", "doc_values": False}}, "doc_values disabled"),
    ],
)
async def test_rrf_mapping_rejects_incompatible_chunk_id(mapping, expected):
    with pytest.raises(RRFMappingError, match=expected):
        await require_sortable_chunk_id_mapping(_Client(mapping), "documents")
