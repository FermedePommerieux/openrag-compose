"""Durable, rollback-safe production canary for metadata-only backfill writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from models.document_metadata import (
    DOCUMENT_METADATA_EXTRACTOR_NAME,
    DOCUMENT_METADATA_EXTRACTOR_VERSION,
    DOCUMENT_METADATA_PROFILE_ID,
    DOCUMENT_METADATA_PROFILE_VERSION,
    DocumentMetadataProfile,
)
from services.document_metadata_backfill import (
    ArchivedOriginalResolver,
    ArchiveManifestEntry,
    DocumentMetadataBackfillJob,
    IndexedDocumentRecord,
    MetadataBackfillStatus,
    exact_occurrence_query,
    map_archived_original,
)
from services.document_metadata_extractor import (
    MetadataExtractionError,
    UnsupportedMetadataFormatError,
    extract_document_metadata,
)

METADATA_FIELDS = (
    "document_metadata_profile",
    "document_metadata_profile_id",
    "document_metadata_profile_version",
    "document_metadata_facts_sha256",
    "document_metadata_extractor",
    "document_metadata_extractor_version",
    "document_metadata_backfill_status",
    "document_metadata_updated_at",
)


class CanaryStatus(StrEnum):
    PENDING = "PENDING"
    EXTRACTED = "EXTRACTED"
    WRITTEN = "WRITTEN"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SKIPPED = "SKIPPED"
    DLS_BLOCKED = "DLS_BLOCKED"
    ARCHIVE_UNAVAILABLE = "ARCHIVE_UNAVAILABLE"


TERMINAL_STATES = {
    CanaryStatus.VERIFIED,
    CanaryStatus.FAILED,
    CanaryStatus.ROLLED_BACK,
    CanaryStatus.SKIPPED,
    CanaryStatus.DLS_BLOCKED,
    CanaryStatus.ARCHIVE_UNAVAILABLE,
}


class InjectedCanaryInterruption(RuntimeError):
    """Test-only crash boundary that intentionally leaves a resumable state."""


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def metadata_state(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "present": {field: source[field] for field in METADATA_FIELDS if field in source},
        "missing": [field for field in METADATA_FIELDS if field not in source],
    }


def expected_metadata_state(
    profile: DocumentMetadataProfile,
    *,
    updated_at: str,
) -> dict[str, Any]:
    values = {
        "document_metadata_profile": profile.model_dump(mode="json"),
        "document_metadata_profile_id": DOCUMENT_METADATA_PROFILE_ID,
        "document_metadata_profile_version": DOCUMENT_METADATA_PROFILE_VERSION,
        "document_metadata_facts_sha256": profile.metadata_facts_sha256,
        "document_metadata_extractor": DOCUMENT_METADATA_EXTRACTOR_NAME,
        "document_metadata_extractor_version": DOCUMENT_METADATA_EXTRACTOR_VERSION,
        "document_metadata_backfill_status": MetadataBackfillStatus.SUCCESS.value,
        "document_metadata_updated_at": updated_at,
    }
    return {"present": values, "missing": []}


class CanaryCheckpoint:
    """Atomic JSON checkpoint containing per-occurrence transitions and rollback state."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {}

    def initialize(
        self,
        *,
        index_name: str,
        records: Iterable[IndexedDocumentRecord],
        run_id: str,
    ) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("index") != index_name:
                raise ValueError("canary checkpoint index differs from requested index")
            return
        items = {
            record.storage_id: {
                "state": CanaryStatus.PENDING.value,
                "record": record.model_dump(mode="json"),
                "history": [
                    {
                        "state": CanaryStatus.PENDING.value,
                        "at": datetime.now(UTC).isoformat(),
                    }
                ],
            }
            for record in records
        }
        self.data = {
            "schema": "openrag.document-metadata-canary-checkpoint",
            "version": 1,
            "run_id": run_id,
            "index": index_name,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "items": items,
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".part")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def transition(self, storage_id: str, state: CanaryStatus, **updates: Any) -> None:
        item = self.data["items"][storage_id]
        item.update(updates)
        item["state"] = state.value
        item.setdefault("history", []).append(
            {"state": state.value, "at": datetime.now(UTC).isoformat()}
        )
        self.data["updated_at"] = datetime.now(UTC).isoformat()
        self.save()


class DocumentMetadataCanary:
    """One-occurrence-at-a-time canary with exact readback and rollback proof."""

    def __init__(
        self,
        client: Any,
        *,
        index_name: str,
        checkpoint: CanaryCheckpoint,
        manifest: dict[str, ArchiveManifestEntry] | None = None,
        local_archive_root: str | os.PathLike[str] | None = None,
        resolver: ArchivedOriginalResolver | None = None,
        failure_injector: Callable[[str, IndexedDocumentRecord], None] | None = None,
    ) -> None:
        self.client = client
        self.index_name = index_name
        self.checkpoint = checkpoint
        self.manifest = manifest or {}
        self.local_archive_root = local_archive_root
        self.resolver = resolver or ArchivedOriginalResolver()
        self.failure_injector = failure_injector
        self.writer = DocumentMetadataBackfillJob(
            client,
            index_name=index_name,
            manifest=self.manifest,
            local_archive_root=local_archive_root,
            resolver=self.resolver,
            write=True,
        )

    def _inject(self, phase: str, record: IndexedDocumentRecord) -> None:
        if self.failure_injector is not None:
            self.failure_injector(phase, record)

    async def run(self) -> dict[str, Any]:
        await self.writer.ensure_mapping()
        for storage_id in list(self.checkpoint.data["items"]):
            item = self.checkpoint.data["items"][storage_id]
            if CanaryStatus(item["state"]) in TERMINAL_STATES:
                continue
            await self.process_item(storage_id)
        return self.summary()

    async def process_item(self, storage_id: str) -> CanaryStatus:
        item = self.checkpoint.data["items"][storage_id]
        state = CanaryStatus(item["state"])
        record = IndexedDocumentRecord.model_validate(item["record"])
        if state is CanaryStatus.WRITTEN:
            return await self._verify(record)
        if state is CanaryStatus.EXTRACTED:
            return await self._resume_extracted(record)
        return await self._extract(record)

    async def _get_representative(self, storage_id: str) -> dict[str, Any]:
        response = await self.client.get(index=self.index_name, id=storage_id)
        if response.get("found") is False or not isinstance(response.get("_source"), dict):
            raise RuntimeError("representative chunk disappeared")
        return response

    async def _occurrence_snapshot(
        self,
        record: IndexedDocumentRecord,
    ) -> dict[str, Any]:
        scope = exact_occurrence_query(record)
        if scope is None:
            raise ValueError("exact occurrence scope unavailable")
        digest = hashlib.sha256()
        chunks = 0
        embeddings = 0
        cursor: list[Any] | None = None
        while True:
            body: dict[str, Any] = {
                "query": scope,
                "_source": {"excludes": list(METADATA_FIELDS)},
                "size": 500,
                "sort": [
                    {"chunk_index": {"order": "asc", "missing": "_last"}},
                    {"chunk_id": {"order": "asc"}},
                ],
            }
            if cursor is not None:
                body["search_after"] = cursor
            response = await self.client.search(
                index=self.index_name,
                body=body,
                request_timeout=300,
            )
            hits = response.get("hits", {}).get("hits", [])
            for hit in hits:
                source = hit.get("_source", {})
                digest.update(_json_sha256(source).encode("ascii"))
                chunks += 1
                if any(
                    key.startswith("chunk_embedding") and value not in (None, [])
                    for key, value in source.items()
                ):
                    embeddings += 1
            if len(hits) < 500:
                break
            cursor = hits[-1].get("sort")
            if not cursor:
                raise RuntimeError("immutable snapshot pagination lost its cursor")
        return {"sha256": digest.hexdigest(), "chunks": chunks, "embeddings": embeddings}

    async def _extract(self, record: IndexedDocumentRecord) -> CanaryStatus:
        read_started = time.perf_counter()
        try:
            representative = await self._get_representative(record.storage_id)
            source = representative["_source"]
            fresh_record = IndexedDocumentRecord.from_hit(
                {"_id": record.storage_id, "_source": source, "sort": record.sort}
            )
            immutable = await self._occurrence_snapshot(fresh_record)
        except Exception as exc:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason=f"pre_write_read_failed:{type(exc).__name__}",
            )
            return CanaryStatus.FAILED
        read_ms = (time.perf_counter() - read_started) * 1000
        lookup_started = time.perf_counter()
        decision = await asyncio.to_thread(
            map_archived_original,
            fresh_record,
            local_archive_root=self.local_archive_root,
            manifest=self.manifest,
        )
        lookup_ms = (time.perf_counter() - lookup_started) * 1000
        if decision.status is MetadataBackfillStatus.NO_ARCHIVE_SOURCE:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.ARCHIVE_UNAVAILABLE,
                reason=decision.reason,
                timings={"archive_lookup_ms": lookup_ms, "opensearch_read_ms": read_ms},
            )
            return CanaryStatus.ARCHIVE_UNAVAILABLE
        if decision.status is not MetadataBackfillStatus.SUCCESS:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason=decision.reason,
                timings={"archive_lookup_ms": lookup_ms, "opensearch_read_ms": read_ms},
            )
            return CanaryStatus.FAILED
        original = None
        resolve_started = time.perf_counter()
        try:
            original = await self.resolver.resolve(fresh_record, decision)
            resolve_ms = (time.perf_counter() - resolve_started) * 1000
            extraction = await asyncio.to_thread(
                extract_document_metadata,
                original.path,
                original.context,
            )
        except UnsupportedMetadataFormatError as exc:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.SKIPPED,
                reason=str(exc),
            )
            return CanaryStatus.SKIPPED
        except (MetadataExtractionError, OSError) as exc:
            integrity_failure = any(
                token in str(exc).casefold()
                for token in ("sha-256", "document_id", "size does not match")
            )
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED if integrity_failure else CanaryStatus.ARCHIVE_UNAVAILABLE,
                reason=(
                    f"archive_integrity_failed:{type(exc).__name__}"
                    if integrity_failure
                    else f"archive_read_failed:{type(exc).__name__}"
                ),
            )
            return CanaryStatus.FAILED if integrity_failure else CanaryStatus.ARCHIVE_UNAVAILABLE
        except Exception as exc:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason=f"extraction_failed:{type(exc).__name__}",
            )
            return CanaryStatus.FAILED
        finally:
            if original is not None:
                original.cleanup()
        scope = exact_occurrence_query(fresh_record)
        if scope is None or not fresh_record.document_chunk_count:
            return self._dls_blocked(fresh_record, "exact_generation_owner_scope_unavailable")
        count_response = await self.client.count(index=self.index_name, body={"query": scope})
        representative_scope = {"bool": {"filter": [scope, {"term": {"chunk_index": 0}}]}}
        representative_count = await self.client.count(
            index=self.index_name, body={"query": representative_scope}
        )
        if int(count_response.get("count", 0)) != fresh_record.document_chunk_count:
            return self._dls_blocked(fresh_record, "exact_scope_chunk_count_mismatch")
        if int(representative_count.get("count", 0)) != 1:
            return self._dls_blocked(fresh_record, "representative_chunk_scope_not_unique")
        updated_at = datetime.now(UTC).isoformat()
        expected = expected_metadata_state(extraction.profile, updated_at=updated_at)
        pre_state = metadata_state(source)
        if (
            pre_state["present"].get("document_metadata_facts_sha256")
            == extraction.profile.metadata_facts_sha256
        ):
            existing_profile_value = pre_state["present"].get("document_metadata_profile")
            try:
                existing_profile = DocumentMetadataProfile.model_validate(existing_profile_value)
            except (TypeError, ValueError):
                existing_profile = None
            controls_valid = {
                "document_metadata_profile_id": DOCUMENT_METADATA_PROFILE_ID,
                "document_metadata_profile_version": DOCUMENT_METADATA_PROFILE_VERSION,
                "document_metadata_extractor": DOCUMENT_METADATA_EXTRACTOR_NAME,
                "document_metadata_extractor_version": DOCUMENT_METADATA_EXTRACTOR_VERSION,
                "document_metadata_backfill_status": MetadataBackfillStatus.SUCCESS.value,
            }
            if (
                existing_profile is not None
                and existing_profile.metadata_facts_sha256
                == extraction.profile.metadata_facts_sha256
                and all(
                    pre_state["present"].get(field) == value
                    for field, value in controls_valid.items()
                )
                and isinstance(pre_state["present"].get("document_metadata_updated_at"), str)
            ):
                expected = pre_state
                self.checkpoint.transition(
                    record.storage_id,
                    CanaryStatus.VERIFIED,
                    reason="unchanged",
                    changed=False,
                    pre_metadata=pre_state,
                    expected_metadata=expected,
                    pre_immutable=immutable,
                    post_immutable=immutable,
                    timings=self._timings(
                        lookup_ms, resolve_ms, read_ms, extraction, write_ms=0, verify_ms=0
                    ),
                )
                return CanaryStatus.VERIFIED
        self.checkpoint.transition(
            record.storage_id,
            CanaryStatus.EXTRACTED,
            record=fresh_record.model_dump(mode="json"),
            reason="ready_to_write",
            changed=True,
            pre_metadata=pre_state,
            expected_metadata=expected,
            pre_immutable=immutable,
            profile=extraction.profile.model_dump(mode="json"),
            representative_scope=representative_scope,
            timings=self._timings(
                lookup_ms, resolve_ms, read_ms, extraction, write_ms=0, verify_ms=0
            ),
            bytes_read=extraction.bytes_read,
            format=extraction.format_name,
        )
        self._inject("after_extraction_before_write", fresh_record)
        return await self._resume_extracted(fresh_record)

    def _dls_blocked(self, record: IndexedDocumentRecord, reason: str) -> CanaryStatus:
        self.checkpoint.transition(
            record.storage_id,
            CanaryStatus.DLS_BLOCKED,
            reason=reason,
        )
        return CanaryStatus.DLS_BLOCKED

    async def _resume_extracted(self, record: IndexedDocumentRecord) -> CanaryStatus:
        item = self.checkpoint.data["items"][record.storage_id]
        current = metadata_state((await self._get_representative(record.storage_id))["_source"])
        if current == item["expected_metadata"]:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.WRITTEN,
                reason="write_observed_during_resume",
            )
            return await self._verify(record)
        if current != item["pre_metadata"]:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason="metadata_changed_outside_canary_before_write",
            )
            return CanaryStatus.FAILED
        profile = DocumentMetadataProfile.model_validate(item["profile"])
        write_started = time.perf_counter()
        try:
            self._inject("before_write", record)
            updated = await self.writer._write_profile(
                item["representative_scope"],
                profile,
                updated_at=item["expected_metadata"]["present"]["document_metadata_updated_at"],
            )
        except InjectedCanaryInterruption:
            raise
        except Exception as exc:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason=f"opensearch_write_failed:{type(exc).__name__}",
            )
            return CanaryStatus.FAILED
        if updated != 1:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason="opensearch_write_count_mismatch",
            )
            return CanaryStatus.FAILED
        write_ms = (time.perf_counter() - write_started) * 1000
        timings = dict(item.get("timings") or {})
        timings["opensearch_write_ms"] = write_ms
        self.checkpoint.transition(
            record.storage_id,
            CanaryStatus.WRITTEN,
            reason="metadata_write_applied",
            timings=timings,
        )
        self._inject("after_write_before_verification", record)
        return await self._verify(record)

    async def _verify(self, record: IndexedDocumentRecord) -> CanaryStatus:
        item = self.checkpoint.data["items"][record.storage_id]
        verify_started = time.perf_counter()
        try:
            self._inject("before_verification_read", record)
            representative = await self._get_representative(record.storage_id)
            observed = metadata_state(representative["_source"])
            immutable = await self._occurrence_snapshot(record)
        except InjectedCanaryInterruption:
            raise
        except Exception as exc:
            # Keep WRITTEN: a restart can retry readback without duplicating a write.
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.WRITTEN,
                reason=f"verification_read_failed:{type(exc).__name__}",
            )
            return CanaryStatus.WRITTEN
        verify_ms = (time.perf_counter() - verify_started) * 1000
        if observed != item["expected_metadata"]:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason="metadata_readback_mismatch",
            )
            return CanaryStatus.FAILED
        if immutable != item["pre_immutable"]:
            self.checkpoint.transition(
                record.storage_id,
                CanaryStatus.FAILED,
                reason="content_chunk_or_embedding_mutation_detected",
                post_immutable=immutable,
            )
            return CanaryStatus.FAILED
        timings = dict(item.get("timings") or {})
        timings["opensearch_verification_read_ms"] = verify_ms
        timings["total_ms"] = sum(value for key, value in timings.items() if key != "total_ms")
        self.checkpoint.transition(
            record.storage_id,
            CanaryStatus.VERIFIED,
            reason="metadata_and_immutable_readback_verified",
            post_metadata=observed,
            post_immutable=immutable,
            timings=timings,
        )
        return CanaryStatus.VERIFIED

    async def rollback(self, storage_ids: Iterable[str]) -> dict[str, int]:
        restored = 0
        failed = 0
        for storage_id in storage_ids:
            item = self.checkpoint.data["items"].get(storage_id)
            if not item or "pre_metadata" not in item:
                failed += 1
                continue
            record = IndexedDocumentRecord.model_validate(item["record"])
            current_immutable = await self._occurrence_snapshot(record)
            if current_immutable != item["pre_immutable"]:
                self.checkpoint.transition(
                    storage_id,
                    CanaryStatus.FAILED,
                    reason="rollback_blocked_by_immutable_mutation",
                )
                failed += 1
                continue
            pre = item["pre_metadata"]
            assignments = "; ".join(
                f"ctx._source['{field}']=params.values['{field}']" for field in pre["present"]
            )
            removals = "; ".join(f"ctx._source.remove('{field}')" for field in pre["missing"])
            script = "; ".join(value for value in (assignments, removals) if value)
            await self.client.update(
                index=self.index_name,
                id=storage_id,
                body={
                    "script": {
                        "lang": "painless",
                        "source": script,
                        "params": {"values": pre["present"]},
                    }
                },
                refresh=True,
                request_timeout=300,
            )
            observed = metadata_state((await self._get_representative(storage_id))["_source"])
            post_immutable = await self._occurrence_snapshot(record)
            if observed != pre or post_immutable != item["pre_immutable"]:
                self.checkpoint.transition(
                    storage_id,
                    CanaryStatus.FAILED,
                    reason="rollback_readback_mismatch",
                )
                failed += 1
                continue
            self.checkpoint.transition(
                storage_id,
                CanaryStatus.ROLLED_BACK,
                reason="exact_pre_canary_metadata_restored",
                rollback_metadata=observed,
                rollback_immutable=post_immutable,
            )
            restored += 1
        return {"restored": restored, "failed": failed}

    @staticmethod
    def _timings(
        lookup_ms: float,
        resolve_ms: float,
        read_ms: float,
        extraction: Any,
        *,
        write_ms: float,
        verify_ms: float,
    ) -> dict[str, float]:
        timings = {
            "archive_lookup_ms": lookup_ms,
            "binary_resolve_ms": resolve_ms,
            "binary_hash_and_context_ms": extraction.context_and_hash_ms,
            "metadata_extraction_ms": extraction.native_extraction_ms,
            "normalization_and_digest_ms": extraction.normalization_and_digest_ms,
            "opensearch_read_ms": read_ms,
            "opensearch_write_ms": write_ms,
            "opensearch_verification_read_ms": verify_ms,
        }
        timings["total_ms"] = sum(timings.values())
        return timings

    def summary(self) -> dict[str, Any]:
        items = list(self.checkpoint.data["items"].values())
        counts: dict[str, int] = {}
        for item in items:
            state = str(item["state"])
            counts[state] = counts.get(state, 0) + 1
        phase_names = sorted({phase for item in items for phase in (item.get("timings") or {})})

        def percentile(values: list[float], quantile: float) -> float:
            ordered = sorted(values)
            if not ordered:
                return 0.0
            index = round((len(ordered) - 1) * quantile)
            return ordered[index]

        timing_stats: dict[str, dict[str, float]] = {}
        for phase in phase_names:
            values = [
                float(item["timings"][phase])
                for item in items
                if phase in (item.get("timings") or {})
            ]
            timing_stats[phase] = {
                "mean_ms": sum(values) / len(values),
                "p50_ms": percentile(values, 0.50),
                "p95_ms": percentile(values, 0.95),
                "p99_ms": percentile(values, 0.99),
                "max_ms": max(values),
            }
        return {
            "schema": "openrag.document-metadata-canary-summary",
            "version": 1,
            "run_id": self.checkpoint.data["run_id"],
            "index": self.index_name,
            "items": len(items),
            "states": dict(sorted(counts.items())),
            "changed": sum(item.get("changed") is True for item in items),
            "unchanged": sum(item.get("changed") is False for item in items),
            "bytes_read": sum(int(item.get("bytes_read") or 0) for item in items),
            "timing_stats": timing_stats,
            "completed_at": datetime.now(UTC).isoformat(),
        }
