"""Fail-closed identity mapping and metadata-only write contract tests."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.document_metadata_backfill import (
    ArchivedOriginalResolver,
    ArchiveManifestEntry,
    DocumentMetadataBackfillJob,
    IndexedDocumentRecord,
    MappingStrength,
    MetadataBackfillStatus,
    exact_occurrence_query,
    load_archive_manifest,
    map_archived_original,
)
from services.scope_traversal_policy import ScopeRelationSemantics, ScopeTraversalPolicy
from services.search_service import redact_dls_opaque_relation_metadata


def _document_id(payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    identifier = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode()[:24]
    return identifier, digest


def _record(document_id: str, **kwargs: object) -> IndexedDocumentRecord:
    values = {
        "storage_id": "chunk-storage-0",
        "sort": ["entity", document_id, "chunk-0"],
        "document_id": document_id,
        "filename": "original.txt",
        "mimetype": "text/plain",
        "source_url": "https://openarchiver.example.test/mail/parent-message",
        "source_entity_id": "urn:openrag:openarchiver:attachment:attachment-1",
        "source_entity_type": "email_attachment",
        "owner": "user-a",
        "ingest_run_id": "run-1",
        "indexed_time": "2026-09-01T10:00:00Z",
        "document_content_sha256": "content-generation-1",
        "document_chunk_count": 2,
    }
    values.update(kwargs)
    return IndexedDocumentRecord.model_validate(values)


def _entry(document_id: str, digest: str, **kwargs: object) -> ArchiveManifestEntry:
    values = {
        "entity_id": "urn:openrag:openarchiver:attachment:attachment-1",
        "archive_source": "openarchiver",
        "archive_object_id": "attachment-1",
        "original_name": "original.txt",
        "storage_path": "mail/parent-message/attachments/attachment-1",
        "expected_sha256": digest,
        "size_bytes": 7,
        "status": "validated",
        "parent_entity_ids": ["urn:openrag:openarchiver:email:parent-message"],
    }
    values.update(kwargs)
    return ArchiveManifestEntry.model_validate(values)


def test_attachment_mail_source_url_is_never_a_binary_mapping_proof():
    document_id, _digest = _document_id(b"payload")
    record = _record(document_id)

    decision = map_archived_original(record, manifest={})

    assert decision.status is MetadataBackfillStatus.NO_ARCHIVE_SOURCE
    assert decision.strength is MappingStrength.NONE
    assert decision.reason == "no_exact_archive_manifest_entry"
    assert "parent-message" not in (decision.archive_object_id or "")


def test_exact_attachment_id_and_hash_manifest_is_strongest_mapping():
    document_id, digest = _document_id(b"payload")
    record = _record(document_id)
    entry = _entry(document_id, digest)

    decision = map_archived_original(record, manifest={entry.entity_id: entry})

    assert decision.status is MetadataBackfillStatus.SUCCESS
    assert decision.strength is MappingStrength.STRONGEST
    assert decision.hash_verified is True
    assert decision.archive_object_id == "attachment-1"


def test_filename_only_and_wrong_hash_are_rejected():
    document_id, digest = _document_id(b"payload")
    record = _record(document_id, filename="same-name.pdf")
    wrong_document_id, _ = _document_id(b"other")
    entry = _entry(document_id, digest, original_name="same-name.pdf")

    absent = map_archived_original(record, manifest={"different-entity": entry})
    wrong_hash = map_archived_original(
        _record(wrong_document_id), manifest={entry.entity_id: entry}
    )

    assert absent.status is MetadataBackfillStatus.NO_ARCHIVE_SOURCE
    assert wrong_hash.status is MetadataBackfillStatus.AMBIGUOUS_SOURCE
    assert wrong_hash.reason == "archive_manifest_content_hash_mismatch"


def test_nonvalidated_archive_registry_entry_is_not_backfillable():
    document_id, digest = _document_id(b"payload")
    entry = _entry(document_id, digest, status="failed")

    decision = map_archived_original(_record(document_id), manifest={entry.entity_id: entry})

    assert decision.status is MetadataBackfillStatus.NO_ARCHIVE_SOURCE
    assert decision.reason == "archive_registry_status_failed"


def test_nonvalidated_manifest_entry_may_preserve_missing_hash():
    document_id, _digest = _document_id(b"payload")
    entry = _entry(document_id, "", status="failed")

    decision = map_archived_original(_record(document_id), manifest={entry.entity_id: entry})

    assert decision.status is MetadataBackfillStatus.NO_ARCHIVE_SOURCE


def test_archive_resolver_creates_configured_private_scratch_directory(tmp_path: Path):
    scratch = tmp_path / "nested" / "downloads"

    ArchivedOriginalResolver(temporary_directory=scratch)

    assert scratch.is_dir()


def test_local_archive_requires_one_hash_matching_regular_file(tmp_path: Path):
    payload = b"local archive payload"
    document_id, _digest = _document_id(payload)
    source_id = f"{document_id}.{'a' * 32}"
    source_directory = tmp_path / source_id
    source_directory.mkdir()
    path = source_directory / "original.txt"
    path.write_bytes(payload)
    record = _record(
        document_id,
        source_url=f"/api/source-files/{source_id}",
        source_entity_id="urn:openrag:local:file-1",
        source_entity_type="file",
    )

    mapped = map_archived_original(record, local_archive_root=tmp_path)
    path.write_bytes(b"changed")
    changed = map_archived_original(record, local_archive_root=tmp_path)

    assert mapped.status is MetadataBackfillStatus.SUCCESS
    assert mapped.strength is MappingStrength.STRONGEST
    assert changed.status is MetadataBackfillStatus.AMBIGUOUS_SOURCE
    assert changed.reason == "local_archive_content_hash_mismatch"


def test_duplicate_local_archive_files_are_ambiguous(tmp_path: Path):
    payload = b"payload"
    document_id, _digest = _document_id(payload)
    source_id = f"{document_id}.{'b' * 32}"
    source_directory = tmp_path / source_id
    source_directory.mkdir()
    (source_directory / "same.txt").write_bytes(payload)
    (source_directory / "same-copy.txt").write_bytes(payload)
    record = _record(document_id, source_url=f"/api/source-files/{source_id}")

    decision = map_archived_original(record, local_archive_root=tmp_path)

    assert decision.status is MetadataBackfillStatus.AMBIGUOUS_SOURCE
    assert decision.reason == "local_archive_file_count_not_one"


def test_manifest_loader_rejects_conflicting_duplicate_entities(tmp_path: Path):
    _document, digest = _document_id(b"payload")
    path = tmp_path / "manifest.json"
    first = _entry("unused", digest).model_dump(mode="json")
    second = {**first, "storage_path": "different/path"}
    path.write_text(__import__("json").dumps([first, second]))

    with pytest.raises(ValueError, match="conflicting archive manifest"):
        load_archive_manifest(path)


def test_exact_occurrence_scope_includes_generation_owner_and_entity():
    document_id, _digest = _document_id(b"payload")
    query = exact_occurrence_query(_record(document_id))

    assert query is not None
    serialized = repr(query)
    assert "document_content_sha256" in serialized
    assert "ingest_run_id" in serialized
    assert "source_entity_id" in serialized
    assert "owner" in serialized


def test_exact_occurrence_scope_fails_closed_without_generation_identity():
    document_id, _digest = _document_id(b"payload")

    assert exact_occurrence_query(_record(document_id, ingest_run_id=None)) is None
    assert exact_occurrence_query(_record(document_id, source_entity_id="", source_url="")) is None


@pytest.mark.asyncio
async def test_dry_run_extracts_but_never_writes_and_second_run_is_unchanged(
    tmp_path: Path,
):
    payload = b"stable content"
    document_id, _digest = _document_id(payload)
    source_id = f"{document_id}.{'c' * 32}"
    source_directory = tmp_path / source_id
    source_directory.mkdir()
    (source_directory / "original.txt").write_bytes(payload)
    record = _record(
        document_id,
        source_url=f"/api/source-files/{source_id}",
        source_entity_id="urn:openrag:local:file-1",
        source_entity_type="file",
    )
    client = MagicMock()
    client.count = AsyncMock(return_value={"count": 2})
    client.update_by_query = AsyncMock()
    job = DocumentMetadataBackfillJob(
        client,
        index_name="documents",
        local_archive_root=tmp_path,
        resolver=ArchivedOriginalResolver(),
        write=False,
    )

    first = await job.process(record)
    second = await job.process(
        record.model_copy(update={"current_metadata_facts_sha256": first.metadata_facts_sha256})
    )

    assert first.status is MetadataBackfillStatus.SUCCESS
    assert first.reason == "dry_run_would_update"
    assert first.chunks_matched == 2
    assert second.status is MetadataBackfillStatus.UNCHANGED
    client.update_by_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_write_allow_list_cannot_mutate_chunks_or_vectors(tmp_path: Path):
    payload = b"stable content"
    document_id, _digest = _document_id(payload)
    source_id = f"{document_id}.{'d' * 32}"
    source_directory = tmp_path / source_id
    source_directory.mkdir()
    (source_directory / "original.txt").write_bytes(payload)
    record = _record(
        document_id,
        source_url=f"/api/source-files/{source_id}",
        source_entity_id="urn:openrag:local:file-1",
        source_entity_type="file",
    )
    client = MagicMock()
    client.count = AsyncMock(side_effect=[{"count": 2}, {"count": 1}])
    client.update_by_query = AsyncMock(return_value={"updated": 1})
    job = DocumentMetadataBackfillJob(
        client,
        index_name="documents",
        local_archive_root=tmp_path,
        write=True,
    )

    result = await job.process(record)

    assert result.status is MetadataBackfillStatus.SUCCESS
    assert result.chunks_matched == 2
    assert result.chunks_updated == 1
    script = client.update_by_query.await_args.kwargs["body"]["script"]
    assert "document_metadata_" in script["source"]
    assert all(
        forbidden not in script["source"]
        for forbidden in ("chunk_id", "chunk_index", "text", "embedding", "vector")
    )
    assert "relations" not in repr(script["params"]["profile"])
    query = client.update_by_query.await_args.kwargs["body"]["query"]
    assert {"term": {"chunk_index": 0}} in query["bool"]["filter"]


@pytest.mark.asyncio
async def test_chunk_count_mismatch_is_dls_blocked(tmp_path: Path):
    payload = b"stable content"
    document_id, _digest = _document_id(payload)
    source_id = f"{document_id}.{'e' * 32}"
    source_directory = tmp_path / source_id
    source_directory.mkdir()
    (source_directory / "original.txt").write_bytes(payload)
    record = _record(document_id, source_url=f"/api/source-files/{source_id}")
    client = SimpleNamespace(count=AsyncMock(return_value={"count": 1}))
    job = DocumentMetadataBackfillJob(client, index_name="documents", local_archive_root=tmp_path)

    result = await job.process(record)

    assert result.status is MetadataBackfillStatus.DLS_BLOCKED
    assert result.reason == "exact_scope_chunk_count_mismatch"


@pytest.mark.asyncio
async def test_nonunique_representative_chunk_is_dls_blocked(tmp_path: Path):
    payload = b"stable content"
    document_id, _digest = _document_id(payload)
    source_id = f"{document_id}.{'f' * 32}"
    source_directory = tmp_path / source_id
    source_directory.mkdir()
    (source_directory / "original.txt").write_bytes(payload)
    record = _record(document_id, source_url=f"/api/source-files/{source_id}")
    client = MagicMock()
    client.count = AsyncMock(side_effect=[{"count": 2}, {"count": 2}])
    client.update_by_query = AsyncMock()
    job = DocumentMetadataBackfillJob(
        client, index_name="documents", local_archive_root=tmp_path, write=True
    )

    result = await job.process(record)

    assert result.status is MetadataBackfillStatus.DLS_BLOCKED
    assert result.reason == "representative_chunk_scope_not_unique"
    client.update_by_query.assert_not_awaited()


def test_metadata_profile_is_recursively_redacted_from_public_results():
    hidden = "urn:openrag:hidden:parent"
    payload = {
        "results": [
            {
                "chunk_id": "visible",
                "document_metadata_profile": {
                    "archive": [
                        {"field": "parent_entity_ids", "value": [hidden]},
                        {"field": "source_path", "value": "/hidden/path"},
                    ]
                },
                "document_metadata_facts_sha256": "secret-digest",
            }
        ]
    }

    redacted = redact_dls_opaque_relation_metadata(payload)

    assert redacted["results"][0] == {"chunk_id": "visible"}
    assert hidden not in repr(redacted)


def test_weak_metadata_similarity_cannot_expand_scope():
    decision = ScopeTraversalPolicy().classify(
        role="same_author_and_date",
        source_type="file",
        target_type="file",
    )

    assert decision.semantics is ScopeRelationSemantics.UNCLASSIFIED
    assert decision.follow_forward is False
    assert decision.follow_reverse is False
    assert decision.certifiable is False
