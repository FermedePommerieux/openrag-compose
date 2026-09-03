"""Bounded, isolated OpenSearch canary for metadata-filter projections."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from models.metadata_filter_projection import (
    MetadataFilterProjectionSideDocument,
    metadata_filter_projection_index_body,
)

MIN_CANARY_DOCUMENTS = 100
MAX_CANARY_DOCUMENTS = 500
_CANARY_INDEX = re.compile(
    r"^documents-metadata-filter-projection-canary-[a-z0-9][a-z0-9-]{0,62}$"
)


class MetadataFilterProjectionCanary:
    """Write only to one validated temporary side index.

    The source corpus index is deliberately not accepted by this class, so a
    projection can never update raw metadata, content, chunks, or vectors.
    """

    def __init__(self, client: Any, *, index_name: str) -> None:
        if _CANARY_INDEX.fullmatch(index_name) is None:
            raise ValueError("canary index name is outside the isolated projection namespace")
        self.client = client
        self.index_name = index_name

    async def create(self) -> None:
        if await self.client.indices.exists(index=self.index_name):
            raise RuntimeError("refusing to reuse an existing canary index")
        await self.client.indices.create(
            index=self.index_name,
            body=metadata_filter_projection_index_body(),
        )

    @staticmethod
    def _validate_documents(
        documents: Sequence[MetadataFilterProjectionSideDocument],
        *,
        enforce_cohort_bounds: bool,
    ) -> None:
        if enforce_cohort_bounds and not MIN_CANARY_DOCUMENTS <= len(documents) <= MAX_CANARY_DOCUMENTS:
            raise ValueError(
                f"canary requires {MIN_CANARY_DOCUMENTS}–{MAX_CANARY_DOCUMENTS} documents"
            )
        ids = [item.projection_document_id for item in documents]
        if len(ids) != len(set(ids)):
            raise ValueError("canary projection ids must be unique")

    async def apply(
        self,
        documents: Sequence[MetadataFilterProjectionSideDocument],
        *,
        enforce_cohort_bounds: bool = True,
    ) -> dict[str, int]:
        """Index only new/changed digests; an identical second run writes zero rows."""
        self._validate_documents(documents, enforce_cohort_bounds=enforce_cohort_bounds)
        response = await self.client.mget(
            index=self.index_name,
            body={"ids": [item.projection_document_id for item in documents]},
        )
        current = {
            str(item.get("_id")): (
                ((item.get("_source") or {}).get("filter") or {}).get("projection_sha256")
            )
            for item in response.get("docs", [])
            if item.get("found")
        }
        changed = [
            item
            for item in documents
            if current.get(item.projection_document_id) != item.filter.projection_sha256
        ]
        if changed:
            body: list[dict[str, Any]] = []
            for item in changed:
                body.append(
                    {
                        "index": {
                            "_index": self.index_name,
                            "_id": item.projection_document_id,
                        }
                    }
                )
                body.append(item.model_dump(mode="json", exclude_none=True))
            bulk = await self.client.bulk(body=body, refresh=True)
            if bulk.get("errors"):
                raise RuntimeError("canary bulk operation reported item errors")
        return {
            "attempted": len(documents),
            "changed": len(changed),
            "unchanged": len(documents) - len(changed),
        }

    async def verify(
        self,
        documents: Sequence[MetadataFilterProjectionSideDocument],
    ) -> dict[str, int]:
        response = await self.client.mget(
            index=self.index_name,
            body={"ids": [item.projection_document_id for item in documents]},
        )
        expected = {
            item.projection_document_id: item.filter.projection_sha256 for item in documents
        }
        verified = 0
        for item in response.get("docs", []):
            source = item.get("_source") or {}
            digest = (source.get("filter") or {}).get("projection_sha256")
            if item.get("found") and expected.get(str(item.get("_id"))) == digest:
                verified += 1
        return {"expected": len(documents), "verified": verified}

    async def rollback(self) -> bool:
        """Delete only the exact isolated canary index and verify its absence."""
        if _CANARY_INDEX.fullmatch(self.index_name) is None:
            raise RuntimeError("rollback target is outside the canary namespace")
        if await self.client.indices.exists(index=self.index_name):
            await self.client.indices.delete(index=self.index_name)
        return not await self.client.indices.exists(index=self.index_name)
