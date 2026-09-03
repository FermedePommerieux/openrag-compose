"""Immutable production generations for metadata-filter projections."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any

from models.metadata_filter_projection import (
    MetadataFilterProjectionSideDocument,
    metadata_filter_projection_index_body,
    metadata_filter_projection_mapping,
)

METADATA_FILTER_PROJECTION_ALIAS = "documents-metadata-filter-current"
_PRODUCTION_INDEX = re.compile(r"^documents-metadata-filter-v1-\d{8}t\d{6}z$")


class MetadataFilterSideIndex:
    """Write one new immutable generation and switch only its stable alias."""

    def __init__(self, client: Any, *, index_name: str) -> None:
        if _PRODUCTION_INDEX.fullmatch(index_name) is None:
            raise ValueError("production side-index name is outside the v1 generation namespace")
        self.client = client
        self.index_name = index_name

    async def create(self, *, shards: int = 1, replicas: int = 0) -> None:
        if await self.client.indices.exists(index=self.index_name):
            raise RuntimeError("refusing to reuse an existing metadata side-index generation")
        body = metadata_filter_projection_index_body(
            number_of_shards=shards,
            number_of_replicas=replicas,
        )
        body["settings"]["index"]["refresh_interval"] = "-1"
        await self.client.indices.create(index=self.index_name, body=body)

    async def apply_batch(
        self,
        documents: Sequence[MetadataFilterProjectionSideDocument],
        *,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        if not documents:
            return {"attempted": 0, "indexed": 0, "failed": 0, "latency_ms": 0.0}
        ids = [item.projection_document_id for item in documents]
        if len(ids) != len(set(ids)):
            raise ValueError("metadata side-index batch contains duplicate projection identities")
        body: list[dict[str, Any]] = []
        for item in documents:
            # Deterministic ids make a retry idempotent if the client loses a
            # response after OpenSearch has already committed the batch.
            body.append(
                {"index": {"_index": self.index_name, "_id": item.projection_document_id}}
            )
            body.append(item.model_dump(mode="json", exclude_none=True))

        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                result = await self.client.bulk(
                    body=body,
                    refresh=False,
                    request_timeout=300,
                )
                failed_items = [
                    operation
                    for item in result.get("items", [])
                    for operation in item.values()
                    if int(operation.get("status") or 500) >= 300
                ]
                if result.get("errors") or failed_items:
                    reasons = sorted(
                        {
                            str((item.get("error") or {}).get("type") or item.get("status"))
                            for item in failed_items
                        }
                    )
                    raise RuntimeError("metadata side-index bulk failed: " + ",".join(reasons))
                return {
                    "attempted": len(documents),
                    "indexed": len(documents),
                    "failed": 0,
                    "attempts": attempt,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            except Exception as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
        raise RuntimeError("metadata side-index batch exhausted bounded retries") from last_error

    async def finalize(self, *, refresh_interval: str = "1s") -> None:
        await self.client.indices.put_settings(
            index=self.index_name,
            body={"index": {"refresh_interval": refresh_interval}},
        )
        await self.client.indices.refresh(index=self.index_name)

    async def verify_mapping(self) -> bool:
        response = await self.client.indices.get_mapping(index=self.index_name)
        actual = (response.get(self.index_name) or {}).get("mappings")
        return actual == metadata_filter_projection_mapping()

    async def current_alias_targets(
        self,
        *,
        alias: str = METADATA_FILTER_PROJECTION_ALIAS,
    ) -> tuple[str, ...]:
        try:
            response = await self.client.indices.get_alias(name=alias)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 404 or "404" in str(exc):
                return ()
            raise
        return tuple(sorted(str(index) for index in response))

    async def switch_alias(
        self,
        *,
        alias: str = METADATA_FILTER_PROJECTION_ALIAS,
    ) -> tuple[str, ...]:
        old_targets = await self.current_alias_targets(alias=alias)
        actions = [
            {"remove": {"index": target, "alias": alias}}
            for target in old_targets
            if target != self.index_name
        ]
        if self.index_name not in old_targets:
            actions.append({"add": {"index": self.index_name, "alias": alias}})
        if actions:
            await self.client.indices.update_aliases(body={"actions": actions})
        if await self.current_alias_targets(alias=alias) != (self.index_name,):
            raise RuntimeError("atomic metadata side-index alias switch did not converge")
        return old_targets

    async def restore_alias(
        self,
        targets: Sequence[str],
        *,
        alias: str = METADATA_FILTER_PROJECTION_ALIAS,
    ) -> None:
        current = await self.current_alias_targets(alias=alias)
        desired = tuple(sorted(set(str(target) for target in targets)))
        actions = [
            {"remove": {"index": target, "alias": alias}}
            for target in current
            if target not in desired
        ]
        actions.extend(
            {"add": {"index": target, "alias": alias}}
            for target in desired
            if target not in current
        )
        if actions:
            await self.client.indices.update_aliases(body={"actions": actions})
        if await self.current_alias_targets(alias=alias) != desired:
            raise RuntimeError("metadata side-index alias rollback did not converge")
