"""Fail-closed, metadata-only enrichment of existing indexed occurrences."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
import urllib.parse
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.document_metadata import (
    DOCUMENT_METADATA_EXTRACTOR_NAME,
    DOCUMENT_METADATA_EXTRACTOR_VERSION,
    DOCUMENT_METADATA_PROFILE_ID,
    DOCUMENT_METADATA_PROFILE_VERSION,
    DocumentMetadataProfile,
    document_metadata_mapping,
)
from services.document_metadata_extractor import (
    ArchiveMetadataContext,
    ExtractionResult,
    MetadataExtractionError,
    UnsupportedMetadataFormatError,
    extract_document_metadata,
)
from services.local_source_service import (
    document_id_from_source_id,
    get_indexed_documents_path,
    source_id_from_local_source_url,
)

DEFAULT_BATCH_SIZE = 25
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 8
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
OPENSEARCH_REQUEST_TIMEOUT_SECONDS = 300


class MetadataBackfillStatus(StrEnum):
    SUCCESS = "SUCCESS"
    UNCHANGED = "UNCHANGED"
    NO_ARCHIVE_SOURCE = "NO_ARCHIVE_SOURCE"
    AMBIGUOUS_SOURCE = "AMBIGUOUS_SOURCE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    DLS_BLOCKED = "DLS_BLOCKED"
    WRITE_FAILED = "WRITE_FAILED"


class MappingStrength(StrEnum):
    NONE = "none"
    WEAK = "weak"
    STRONG = "strong_stable_archive_id"
    STRONGEST = "strongest_content_hash_verified"


class IndexedDocumentRecord(BaseModel):
    """One representative chunk for one indexed source occurrence."""

    model_config = ConfigDict(extra="ignore")

    storage_id: str
    sort: list[Any]
    document_id: str = Field(min_length=1)
    filename: str = ""
    mimetype: str = ""
    file_size: int | None = None
    source_url: str = ""
    source_entity_id: str = ""
    source_entity_type: str = ""
    source_provenance: dict[str, Any] | None = None
    owner: str | None = None
    ingest_run_id: str | None = None
    indexed_time: str | None = None
    document_content_sha256: str | None = None
    document_chunk_count: int | None = None
    current_metadata_facts_sha256: str | None = None

    @property
    def occurrence_id(self) -> str:
        return self.source_entity_id or self.source_url or f"document:{self.document_id}"

    @classmethod
    def from_hit(cls, hit: dict[str, Any]) -> IndexedDocumentRecord:
        source = hit.get("_source", {})
        return cls(
            storage_id=str(hit.get("_id") or ""),
            sort=list(hit.get("sort") or []),
            document_id=str(source.get("document_id") or ""),
            filename=str(source.get("filename") or ""),
            mimetype=str(source.get("mimetype") or ""),
            file_size=source.get("file_size") if isinstance(source.get("file_size"), int) else None,
            source_url=str(source.get("source_url") or ""),
            source_entity_id=str(source.get("source_entity_id") or ""),
            source_entity_type=str(source.get("source_entity_type") or ""),
            source_provenance=(
                source.get("source_provenance")
                if isinstance(source.get("source_provenance"), dict)
                else None
            ),
            owner=str(source["owner"]) if source.get("owner") is not None else None,
            ingest_run_id=(str(source["ingest_run_id"]) if source.get("ingest_run_id") else None),
            indexed_time=(str(source["indexed_time"]) if source.get("indexed_time") else None),
            document_content_sha256=(
                str(source["document_content_sha256"])
                if source.get("document_content_sha256")
                else None
            ),
            document_chunk_count=(
                int(source["document_chunk_count"])
                if isinstance(source.get("document_chunk_count"), int)
                else None
            ),
            current_metadata_facts_sha256=(
                str(source["document_metadata_facts_sha256"])
                if source.get("document_metadata_facts_sha256")
                else None
            ),
        )


class ArchiveManifestEntry(BaseModel):
    """Exact archive locator exported from an authoritative archive registry."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    archive_source: str
    archive_object_id: str
    original_name: str
    storage_path: str
    expected_sha256: str = ""
    size_bytes: int | None = None
    status: str = "validated"
    archived_at: str | None = None
    archive_created_at: str | None = None
    archive_modified_at: str | None = None
    filesystem_birthtime: str | None = None
    filesystem_mtime: str | None = None
    filesystem_ctime: str | None = None
    parent_entity_ids: list[str] = Field(default_factory=list)

    @field_validator("expected_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            return ""
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("expected_sha256 must be a complete hexadecimal SHA-256")
        return normalized

    @model_validator(mode="after")
    def validated_entries_require_sha256(self) -> ArchiveManifestEntry:
        if self.status == "validated" and not self.expected_sha256:
            raise ValueError("validated archive entries require a complete SHA-256")
        return self


@dataclass(frozen=True)
class MappingDecision:
    status: MetadataBackfillStatus
    strength: MappingStrength
    reason: str
    archive_source: str | None = None
    archive_object_id: str | None = None
    path: Path | None = None
    manifest: ArchiveManifestEntry | None = None
    hash_verified: bool = False


@dataclass
class ResolvedOriginal:
    path: Path
    context: ArchiveMetadataContext
    sha256: str
    temporary: bool = False

    def cleanup(self) -> None:
        if self.temporary:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class BackfillDocumentResult:
    occurrence_id: str
    document_id: str
    status: MetadataBackfillStatus
    mapping_strength: MappingStrength
    reason: str
    metadata_facts_sha256: str | None = None
    format_name: str | None = None
    extraction_ms: float | None = None
    bytes_read: int = 0
    conflicts: int = 0
    chunks_matched: int = 0
    chunks_updated: int = 0
    profile: dict[str, Any] | None = None


def load_archive_manifest(path: str | os.PathLike[str]) -> dict[str, ArchiveManifestEntry]:
    """Load JSON/JSONL and reject conflicting duplicate entity identities."""
    source = Path(path)
    if source.suffix.lower() == ".jsonl":
        values = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    else:
        payload = json.loads(source.read_text())
        values = payload.get("entries", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("archive manifest must contain a list of entries")
    result: dict[str, ArchiveManifestEntry] = {}
    for raw in values:
        entry = ArchiveManifestEntry.model_validate(raw)
        previous = result.get(entry.entity_id)
        if previous is not None and previous != entry:
            raise ValueError(f"conflicting archive manifest entries for {entry.entity_id}")
        result[entry.entity_id] = entry
    return result


def _full_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _document_id_from_sha256(value: str) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(value)).rstrip(b"=").decode()[:24]


def map_archived_original(
    record: IndexedDocumentRecord,
    *,
    local_archive_root: str | os.PathLike[str] | None = None,
    manifest: dict[str, ArchiveManifestEntry] | None = None,
    verify_local_hash: bool = True,
) -> MappingDecision:
    """Map by stable ids and hashes; filenames never participate in admission."""
    source_id = source_id_from_local_source_url(record.source_url)
    if source_id is not None:
        document_id = document_id_from_source_id(source_id)
        if document_id != record.document_id:
            return MappingDecision(
                MetadataBackfillStatus.AMBIGUOUS_SOURCE,
                MappingStrength.NONE,
                "local_archive_id_document_id_mismatch",
                archive_source="openrag_local_archive",
                archive_object_id=source_id,
            )
        root = Path(local_archive_root or get_indexed_documents_path()).expanduser().resolve()
        directory = root / source_id
        if directory.is_symlink() or not directory.is_dir():
            return MappingDecision(
                MetadataBackfillStatus.NO_ARCHIVE_SOURCE,
                MappingStrength.STRONG,
                "local_archive_directory_missing",
                archive_source="openrag_local_archive",
                archive_object_id=source_id,
            )
        candidates = [
            value
            for value in directory.iterdir()
            if value.is_file()
            and not value.is_symlink()
            and value.resolve().parent == directory.resolve()
        ]
        if len(candidates) != 1:
            return MappingDecision(
                MetadataBackfillStatus.AMBIGUOUS_SOURCE,
                MappingStrength.STRONG,
                "local_archive_file_count_not_one",
                archive_source="openrag_local_archive",
                archive_object_id=source_id,
            )
        path = candidates[0].resolve()
        if not verify_local_hash:
            return MappingDecision(
                MetadataBackfillStatus.SUCCESS,
                MappingStrength.STRONG,
                "stable_local_archive_id",
                archive_source="openrag_local_archive",
                archive_object_id=source_id,
                path=path,
            )
        sha256, _size = _full_sha256(path)
        if _document_id_from_sha256(sha256) != record.document_id:
            return MappingDecision(
                MetadataBackfillStatus.AMBIGUOUS_SOURCE,
                MappingStrength.STRONG,
                "local_archive_content_hash_mismatch",
                archive_source="openrag_local_archive",
                archive_object_id=source_id,
                path=path,
            )
        return MappingDecision(
            MetadataBackfillStatus.SUCCESS,
            MappingStrength.STRONGEST,
            "local_archive_content_hash_verified",
            archive_source="openrag_local_archive",
            archive_object_id=source_id,
            path=path,
            hash_verified=True,
        )

    entry = (manifest or {}).get(record.source_entity_id)
    if entry is None:
        # A mail-facing source_url on an attachment is intentionally ignored:
        # it identifies the UI context, not the attachment binary.
        return MappingDecision(
            MetadataBackfillStatus.NO_ARCHIVE_SOURCE,
            MappingStrength.NONE,
            "no_exact_archive_manifest_entry",
        )
    if entry.entity_id != record.source_entity_id:
        return MappingDecision(
            MetadataBackfillStatus.AMBIGUOUS_SOURCE,
            MappingStrength.NONE,
            "archive_manifest_entity_id_mismatch",
        )
    if entry.status != "validated":
        return MappingDecision(
            MetadataBackfillStatus.NO_ARCHIVE_SOURCE,
            MappingStrength.STRONG,
            f"archive_registry_status_{entry.status}",
            archive_source=entry.archive_source,
            archive_object_id=entry.archive_object_id,
            manifest=entry,
        )
    if _document_id_from_sha256(entry.expected_sha256) != record.document_id:
        return MappingDecision(
            MetadataBackfillStatus.AMBIGUOUS_SOURCE,
            MappingStrength.STRONG,
            "archive_manifest_content_hash_mismatch",
            archive_source=entry.archive_source,
            archive_object_id=entry.archive_object_id,
            manifest=entry,
        )
    return MappingDecision(
        MetadataBackfillStatus.SUCCESS,
        MappingStrength.STRONGEST,
        "archive_manifest_content_hash_verified",
        archive_source=entry.archive_source,
        archive_object_id=entry.archive_object_id,
        manifest=entry,
        hash_verified=True,
    )


class ArchivedOriginalResolver:
    """Resolve local originals or re-download manifest-addressed originals."""

    def __init__(
        self,
        *,
        openarchiver_base_url: str | None = None,
        openarchiver_api_key_file: str | os.PathLike[str] | None = None,
        temporary_directory: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 120,
        download_attempts: int = 3,
    ) -> None:
        self.openarchiver_base_url = (openarchiver_base_url or "").rstrip("/")
        self.openarchiver_api_key_file = (
            Path(openarchiver_api_key_file) if openarchiver_api_key_file else None
        )
        self.temporary_directory = Path(temporary_directory) if temporary_directory else None
        self.timeout_seconds = timeout_seconds
        if download_attempts < 1 or download_attempts > 5:
            raise ValueError("download_attempts must be between 1 and 5")
        self.download_attempts = download_attempts

    async def resolve(
        self,
        record: IndexedDocumentRecord,
        decision: MappingDecision,
    ) -> ResolvedOriginal:
        if decision.status != MetadataBackfillStatus.SUCCESS:
            raise MetadataExtractionError("cannot resolve a fail-closed mapping decision")
        if decision.path is not None:
            sha256, _size = await asyncio.to_thread(_full_sha256, decision.path)
            if _document_id_from_sha256(sha256) != record.document_id:
                raise MetadataExtractionError("local original changed after mapping verification")
            return ResolvedOriginal(
                path=decision.path,
                sha256=sha256,
                context=self._context(record, decision, decision.path.name),
            )
        if decision.manifest is None:
            raise MetadataExtractionError("remote mapping has no archive manifest")
        return await asyncio.to_thread(self._download_manifest_entry, record, decision)

    def _context(
        self,
        record: IndexedDocumentRecord,
        decision: MappingDecision,
        original_name: str,
    ) -> ArchiveMetadataContext:
        manifest = decision.manifest
        if manifest is None:
            archive_storage_locator = f"{decision.archive_object_id}/{original_name}"
            parent_ids: tuple[str, ...] = ()
            archived_at = archive_created_at = archive_modified_at = None
        else:
            # Store an archive-relative locator only; never persist a host path.
            archive_storage_locator = manifest.storage_path
            parent_ids = tuple(manifest.parent_entity_ids)
            archived_at = manifest.archived_at
            archive_created_at = manifest.archive_created_at
            archive_modified_at = manifest.archive_modified_at
        return ArchiveMetadataContext(
            entity_id=record.source_entity_id or record.source_url,
            archive_source=str(decision.archive_source or "unknown"),
            archive_object_id=str(decision.archive_object_id or ""),
            original_name=original_name,
            archive_storage_locator=archive_storage_locator,
            mime_type=record.mimetype or None,
            archived_at=archived_at,
            archive_created_at=archive_created_at,
            archive_modified_at=archive_modified_at,
            filesystem_birthtime=(manifest.filesystem_birthtime if manifest else None),
            filesystem_mtime=(manifest.filesystem_mtime if manifest else None),
            filesystem_ctime=(manifest.filesystem_ctime if manifest else None),
            ingested_at=record.indexed_time,
            parent_entity_ids=parent_ids,
        )

    def _download_manifest_entry(
        self,
        record: IndexedDocumentRecord,
        decision: MappingDecision,
    ) -> ResolvedOriginal:
        for attempt in range(self.download_attempts):
            try:
                return self._download_manifest_entry_once(record, decision)
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                if not retryable or attempt + 1 >= self.download_attempts:
                    raise MetadataExtractionError(
                        f"archive download returned HTTP {exc.response.status_code}"
                    ) from exc
            except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
                if attempt + 1 >= self.download_attempts:
                    raise MetadataExtractionError("archive download exhausted retries") from exc
            time.sleep(min(2**attempt, 4))
        raise MetadataExtractionError("archive download exhausted retries")

    def _download_manifest_entry_once(
        self,
        record: IndexedDocumentRecord,
        decision: MappingDecision,
    ) -> ResolvedOriginal:
        entry = decision.manifest
        assert entry is not None
        if not self.openarchiver_base_url or self.openarchiver_api_key_file is None:
            raise MetadataExtractionError("OpenArchiver download configuration is unavailable")
        api_key = self.openarchiver_api_key_file.read_text().strip()
        if not api_key:
            raise MetadataExtractionError("OpenArchiver API key is empty")
        suffix = Path(entry.original_name).suffix
        handle = tempfile.NamedTemporaryFile(
            prefix="openrag-metadata-",
            suffix=suffix,
            dir=self.temporary_directory,
            delete=False,
        )
        path = Path(handle.name)
        os.chmod(path, 0o600)
        digest = hashlib.sha256()
        total = 0
        url = (
            self.openarchiver_base_url
            + "/storage/download?"
            + urllib.parse.urlencode({"path": entry.storage_path})
        )
        try:
            with handle, httpx.Client(timeout=self.timeout_seconds) as client:
                with client.stream(
                    "GET",
                    url,
                    headers={"X-API-KEY": api_key, "Accept": "application/octet-stream"},
                ) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise MetadataExtractionError("archive download exceeds the v1 limit")
                        digest.update(chunk)
                        handle.write(chunk)
            observed = digest.hexdigest()
            if observed != entry.expected_sha256:
                raise MetadataExtractionError("archive download SHA-256 does not match manifest")
            if entry.size_bytes is not None and total != entry.size_bytes:
                raise MetadataExtractionError("archive download size does not match manifest")
            if _document_id_from_sha256(observed) != record.document_id:
                raise MetadataExtractionError("archive download does not match indexed document_id")
            return ResolvedOriginal(
                path=path,
                sha256=observed,
                temporary=True,
                context=self._context(record, decision, entry.original_name),
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise


async def scan_indexed_documents(
    client: Any,
    *,
    index_name: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    search_after: list[Any] | None = None,
) -> AsyncIterator[IndexedDocumentRecord]:
    """Scan exact representative chunks with resumable search_after ordering."""
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    cursor = search_after
    while True:
        body: dict[str, Any] = {
            "query": {"bool": {"filter": [{"term": {"chunk_index": 0}}]}},
            "_source": [
                "document_id",
                "filename",
                "mimetype",
                "file_size",
                "source_url",
                "source_entity_id",
                "source_entity_type",
                "source_provenance",
                "owner",
                "ingest_run_id",
                "indexed_time",
                "document_content_sha256",
                "document_chunk_count",
                "document_metadata_facts_sha256",
            ],
            "size": batch_size,
            "track_total_hits": False,
            "sort": [
                {"source_entity_id": {"order": "asc", "missing": "_last"}},
                {"source_url": {"order": "asc", "missing": "_last"}},
                {"document_id": {"order": "asc"}},
                {"chunk_id": {"order": "asc"}},
            ],
        }
        if cursor is not None:
            body["search_after"] = cursor
        response = await client.search(
            index=index_name,
            body=body,
            request_timeout=OPENSEARCH_REQUEST_TIMEOUT_SECONDS,
        )
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            yield IndexedDocumentRecord.from_hit(hit)
        cursor = hits[-1].get("sort")
        if not cursor or len(hits) < batch_size:
            break


def exact_occurrence_query(record: IndexedDocumentRecord) -> dict[str, Any] | None:
    """Build the immutable generation/owner scope or refuse an unsafe write."""
    if not record.document_id or not record.document_content_sha256 or not record.ingest_run_id:
        return None
    filters: list[dict[str, Any]] = [
        {"term": {"document_id": record.document_id}},
        {"term": {"document_content_sha256": record.document_content_sha256}},
        {"term": {"ingest_run_id": record.ingest_run_id}},
    ]
    if record.source_entity_id:
        filters.append({"term": {"source_entity_id": record.source_entity_id}})
    elif record.source_url:
        filters.append({"term": {"source_url": record.source_url}})
    else:
        return None
    if record.owner is None:
        filters.append({"bool": {"must_not": {"exists": {"field": "owner"}}}})
    else:
        filters.append({"term": {"owner": record.owner}})
    return {"bool": {"filter": filters}}


class DocumentMetadataBackfillJob:
    """Bounded runner; dry-run is the default and writes are metadata-only."""

    def __init__(
        self,
        client: Any,
        *,
        index_name: str,
        manifest: dict[str, ArchiveManifestEntry] | None = None,
        local_archive_root: str | os.PathLike[str] | None = None,
        resolver: ArchivedOriginalResolver | None = None,
        write: bool = False,
    ) -> None:
        self.client = client
        self.index_name = index_name
        self.manifest = manifest or {}
        self.local_archive_root = local_archive_root
        self.resolver = resolver or ArchivedOriginalResolver()
        self.write = write

    async def ensure_mapping(self) -> None:
        if self.write:
            await self.client.indices.put_mapping(
                index=self.index_name,
                body={"properties": document_metadata_mapping()},
            )

    async def process(self, record: IndexedDocumentRecord) -> BackfillDocumentResult:
        decision = await asyncio.to_thread(
            map_archived_original,
            record,
            local_archive_root=self.local_archive_root,
            manifest=self.manifest,
        )
        if decision.status != MetadataBackfillStatus.SUCCESS:
            return BackfillDocumentResult(
                occurrence_id=record.occurrence_id,
                document_id=record.document_id,
                status=decision.status,
                mapping_strength=decision.strength,
                reason=decision.reason,
            )
        original: ResolvedOriginal | None = None
        try:
            original = await self.resolver.resolve(record, decision)
            extraction = await asyncio.to_thread(
                extract_document_metadata,
                original.path,
                original.context,
            )
        except UnsupportedMetadataFormatError as exc:
            return self._failed(record, decision, MetadataBackfillStatus.UNSUPPORTED_FORMAT, exc)
        except MetadataExtractionError as exc:
            return self._failed(record, decision, MetadataBackfillStatus.EXTRACTION_FAILED, exc)
        except ValueError as exc:
            return self._failed(record, decision, MetadataBackfillStatus.NORMALIZATION_FAILED, exc)
        except Exception as exc:
            return self._failed(record, decision, MetadataBackfillStatus.EXTRACTION_FAILED, exc)
        finally:
            if original is not None:
                original.cleanup()

        digest = extraction.profile.metadata_facts_sha256
        if digest == record.current_metadata_facts_sha256:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.UNCHANGED,
                "canonical_metadata_digest_unchanged",
            )
        scope = exact_occurrence_query(record)
        if scope is None or not record.document_chunk_count:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.DLS_BLOCKED,
                "exact_generation_owner_scope_unavailable",
            )
        count_response = await self.client.count(index=self.index_name, body={"query": scope})
        matched = int(count_response.get("count", 0))
        if matched != record.document_chunk_count:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.DLS_BLOCKED,
                "exact_scope_chunk_count_mismatch",
                chunks_matched=matched,
            )
        if not self.write:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.SUCCESS,
                "dry_run_would_update",
                chunks_matched=matched,
            )
        representative_scope = {
            "bool": {
                "filter": [
                    scope,
                    {"term": {"chunk_index": 0}},
                ]
            }
        }
        representative_count = await self.client.count(
            index=self.index_name,
            body={"query": representative_scope},
        )
        if int(representative_count.get("count", 0)) != 1:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.DLS_BLOCKED,
                "representative_chunk_scope_not_unique",
                chunks_matched=matched,
            )
        try:
            updated = await self._write_profile(representative_scope, extraction.profile)
        except Exception as exc:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.WRITE_FAILED,
                f"metadata_update_failed:{type(exc).__name__}",
                chunks_matched=matched,
            )
        if updated != 1:
            return self._result(
                record,
                decision,
                extraction,
                MetadataBackfillStatus.WRITE_FAILED,
                "metadata_update_chunk_count_mismatch",
                chunks_matched=matched,
                chunks_updated=updated,
            )
        return self._result(
            record,
            decision,
            extraction,
            MetadataBackfillStatus.SUCCESS,
            "metadata_only_update_applied",
            chunks_matched=matched,
            chunks_updated=updated,
        )

    async def _write_profile(
        self,
        query: dict[str, Any],
        profile: DocumentMetadataProfile,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        result = await self.client.update_by_query(
            index=self.index_name,
            body={
                "query": query,
                "script": {
                    "lang": "painless",
                    # Intentionally exhaustive allow-list: no content, chunk,
                    # graph, ranking, or vector field can be assigned here.
                    "source": (
                        "ctx._source.document_metadata_profile=params.profile; "
                        "ctx._source.document_metadata_profile_id=params.profile_id; "
                        "ctx._source.document_metadata_profile_version=params.profile_version; "
                        "ctx._source.document_metadata_facts_sha256=params.digest; "
                        "ctx._source.document_metadata_extractor=params.extractor; "
                        "ctx._source.document_metadata_extractor_version=params.extractor_version; "
                        "ctx._source.document_metadata_backfill_status=params.status; "
                        "ctx._source.document_metadata_updated_at=params.updated_at"
                    ),
                    "params": {
                        "profile": profile.model_dump(mode="json"),
                        "profile_id": DOCUMENT_METADATA_PROFILE_ID,
                        "profile_version": DOCUMENT_METADATA_PROFILE_VERSION,
                        "digest": profile.metadata_facts_sha256,
                        "extractor": DOCUMENT_METADATA_EXTRACTOR_NAME,
                        "extractor_version": DOCUMENT_METADATA_EXTRACTOR_VERSION,
                        "status": MetadataBackfillStatus.SUCCESS.value,
                        "updated_at": now,
                    },
                },
            },
            refresh=False,
            conflicts="abort",
            request_timeout=OPENSEARCH_REQUEST_TIMEOUT_SECONDS,
        )
        return int(result.get("updated", 0))

    @staticmethod
    def _failed(
        record: IndexedDocumentRecord,
        decision: MappingDecision,
        status: MetadataBackfillStatus,
        error: Exception,
    ) -> BackfillDocumentResult:
        return BackfillDocumentResult(
            occurrence_id=record.occurrence_id,
            document_id=record.document_id,
            status=status,
            mapping_strength=decision.strength,
            reason=f"{type(error).__name__}:{error}",
        )

    @staticmethod
    def _result(
        record: IndexedDocumentRecord,
        decision: MappingDecision,
        extraction: ExtractionResult,
        status: MetadataBackfillStatus,
        reason: str,
        *,
        chunks_matched: int = 0,
        chunks_updated: int = 0,
    ) -> BackfillDocumentResult:
        return BackfillDocumentResult(
            occurrence_id=record.occurrence_id,
            document_id=record.document_id,
            status=status,
            mapping_strength=decision.strength,
            reason=reason,
            metadata_facts_sha256=extraction.profile.metadata_facts_sha256,
            format_name=extraction.format_name,
            extraction_ms=extraction.elapsed_ms,
            bytes_read=extraction.bytes_read,
            conflicts=len(extraction.profile.conflicts),
            chunks_matched=chunks_matched,
            chunks_updated=chunks_updated,
            profile=extraction.profile.model_dump(mode="json"),
        )


async def bounded_process(
    job: DocumentMetadataBackfillJob,
    records: Iterable[IndexedDocumentRecord],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[BackfillDocumentResult]:
    """Process one caller-bounded batch with Pi-friendly concurrency."""
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(record: IndexedDocumentRecord) -> BackfillDocumentResult:
        async with semaphore:
            return await job.process(record)

    return list(await asyncio.gather(*(one(record) for record in records)))
