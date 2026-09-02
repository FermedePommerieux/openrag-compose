from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from services.document_metadata_backfill import ArchivedOriginalResolver, IndexedDocumentRecord
from services.document_metadata_canary import (
    METADATA_FIELDS,
    CanaryCheckpoint,
    CanaryStatus,
    DocumentMetadataCanary,
    InjectedCanaryInterruption,
)
from services.document_metadata_extractor import MetadataExtractionError


class FakeIndices:
    async def put_mapping(self, **_kwargs: Any) -> None:
        return None


class FakeOpenSearch:
    def __init__(self, record: IndexedDocumentRecord) -> None:
        base = {
            "document_id": record.document_id,
            "filename": record.filename,
            "mimetype": record.mimetype,
            "file_size": record.file_size,
            "source_url": record.source_url,
            "source_entity_id": record.source_entity_id,
            "source_entity_type": record.source_entity_type,
            "owner": record.owner,
            "ingest_run_id": record.ingest_run_id,
            "indexed_time": record.indexed_time,
            "document_content_sha256": record.document_content_sha256,
            "document_chunk_count": 2,
        }
        self.documents = {
            record.storage_id: {
                **base,
                "chunk_id": "chunk-0",
                "chunk_index": 0,
                "text": "first",
                "chunk_embedding_test": [0.1, 0.2],
            },
            "storage-1": {
                **base,
                "chunk_id": "chunk-1",
                "chunk_index": 1,
                "text": "second",
                "chunk_embedding_test": [0.3, 0.4],
            },
        }
        self.indices = FakeIndices()
        self.writes = 0
        self.fail_write = False
        self.fail_next_search = False

    async def get(self, *, index: str, id: str) -> dict[str, Any]:
        del index
        return {"found": True, "_id": id, "_source": dict(self.documents[id])}

    async def search(self, *, index: str, body: dict[str, Any], **_kwargs: Any) -> dict:
        del index
        if self.fail_next_search:
            self.fail_next_search = False
            raise RuntimeError("readback unavailable")
        excludes = body.get("_source", {}).get("excludes", [])
        hits = []
        for storage_id, source in sorted(
            self.documents.items(),
            key=lambda item: cast(int, item[1]["chunk_index"]),
        ):
            projected = {key: value for key, value in source.items() if key not in excludes}
            hits.append(
                {
                    "_id": storage_id,
                    "_source": projected,
                    "sort": [source["chunk_index"], source["chunk_id"]],
                }
            )
        return {"hits": {"hits": hits}}

    async def count(self, *, index: str, body: dict[str, Any]) -> dict[str, int]:
        del index
        serialized = repr(body)
        return {"count": 1 if "chunk_index" in serialized else 2}

    async def update_by_query(self, *, index: str, body: dict, **_kwargs: Any) -> dict:
        del index
        if self.fail_write:
            raise RuntimeError("write unavailable")
        self.writes += 1
        params = body["script"]["params"]
        representative = self.documents["storage-0"]
        representative.update(
            {
                "document_metadata_profile": params["profile"],
                "document_metadata_profile_id": params["profile_id"],
                "document_metadata_profile_version": params["profile_version"],
                "document_metadata_facts_sha256": params["digest"],
                "document_metadata_extractor": params["extractor"],
                "document_metadata_extractor_version": params["extractor_version"],
                "document_metadata_backfill_status": params["status"],
                "document_metadata_updated_at": params["updated_at"],
            }
        )
        return {"updated": 1}

    async def update(self, *, index: str, id: str, body: dict, **_kwargs: Any) -> dict:
        del index
        values = body["script"]["params"]["values"]
        for field in METADATA_FIELDS:
            self.documents[id].pop(field, None)
        self.documents[id].update(values)
        return {"result": "updated"}


def _record_and_archive(tmp_path: Path) -> tuple[IndexedDocumentRecord, Path]:
    payload = b"stable canary content"
    sha256 = hashlib.sha256(payload).hexdigest()
    document_id = base64.urlsafe_b64encode(bytes.fromhex(sha256)).rstrip(b"=").decode()[:24]
    source_id = f"{document_id}.{'a' * 32}"
    directory = tmp_path / source_id
    directory.mkdir()
    (directory / "original.txt").write_bytes(payload)
    return (
        IndexedDocumentRecord(
            storage_id="storage-0",
            sort=["entity", document_id, "chunk-0"],
            document_id=document_id,
            filename="original.txt",
            mimetype="text/plain",
            file_size=len(payload),
            source_url=f"/api/source-files/{source_id}",
            source_entity_id="urn:openrag:local:file-1",
            source_entity_type="file",
            owner="owner-1",
            ingest_run_id="run-1",
            indexed_time="2026-09-02T00:00:00Z",
            document_content_sha256="content-generation-1",
            document_chunk_count=2,
        ),
        directory,
    )


def _canary(
    tmp_path: Path,
    client: FakeOpenSearch,
    record: IndexedDocumentRecord,
    *,
    name: str,
    injector=None,
) -> DocumentMetadataCanary:
    checkpoint = CanaryCheckpoint(tmp_path / f"{name}.json")
    checkpoint.initialize(index_name="documents", records=[record], run_id=name)
    return DocumentMetadataCanary(
        client,
        index_name="documents",
        checkpoint=checkpoint,
        local_archive_root=tmp_path,
        failure_injector=injector,
    )


@pytest.mark.asyncio
async def test_canary_writes_verifies_and_second_run_changes_zero(tmp_path: Path):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)
    first = _canary(tmp_path, client, record, name="first")

    first_summary = await first.run()
    second = _canary(tmp_path, client, record, name="second")
    second_summary = await second.run()

    assert first_summary["states"] == {"VERIFIED": 1}
    assert first_summary["changed"] == 1
    assert first_summary["bytes_read"] == len(b"stable canary content")
    assert second_summary["states"] == {"VERIFIED": 1}
    assert second_summary["changed"] == 0
    assert second_summary["bytes_read"] == len(b"stable canary content")
    assert client.writes == 1
    item = first.checkpoint.data["items"][record.storage_id]
    assert item["pre_immutable"] == item["post_immutable"]
    assert item["pre_immutable"]["chunks"] == 2
    assert item["pre_immutable"]["embeddings"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "resumable_state", "expected_writes"),
    [
        ("after_extraction_before_write", CanaryStatus.EXTRACTED, 1),
        ("after_write_before_verification", CanaryStatus.WRITTEN, 1),
    ],
)
async def test_canary_resumes_both_process_interruption_boundaries(
    tmp_path: Path,
    phase: str,
    resumable_state: CanaryStatus,
    expected_writes: int,
):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)

    def interrupt(observed_phase: str, _record: IndexedDocumentRecord) -> None:
        if observed_phase == phase:
            raise InjectedCanaryInterruption(phase)

    canary = _canary(tmp_path, client, record, name=phase, injector=interrupt)
    with pytest.raises(InjectedCanaryInterruption):
        await canary.run()
    assert (
        CanaryStatus(canary.checkpoint.data["items"][record.storage_id]["state"]) is resumable_state
    )

    resumed = DocumentMetadataCanary(
        client,
        index_name="documents",
        checkpoint=canary.checkpoint,
        local_archive_root=tmp_path,
    )
    summary = await resumed.run()

    assert summary["states"] == {"VERIFIED": 1}
    assert client.writes == expected_writes


@pytest.mark.asyncio
async def test_canary_write_failure_is_explicit_and_does_not_mutate(tmp_path: Path):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)
    client.fail_write = True
    canary = _canary(tmp_path, client, record, name="write-failure")

    summary = await canary.run()

    assert summary["states"] == {"FAILED": 1}
    assert client.writes == 0
    assert "opensearch_write_failed" in canary.checkpoint.data["items"][record.storage_id]["reason"]


@pytest.mark.asyncio
async def test_canary_readback_failure_remains_written_and_resumes_without_rewrite(
    tmp_path: Path,
):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)

    def fail_read(phase: str, _record: IndexedDocumentRecord) -> None:
        if phase == "before_verification_read":
            client.fail_next_search = True

    canary = _canary(tmp_path, client, record, name="read-failure", injector=fail_read)
    summary = await canary.run()
    assert summary["states"] == {"WRITTEN": 1}

    resumed = DocumentMetadataCanary(
        client,
        index_name="documents",
        checkpoint=canary.checkpoint,
        local_archive_root=tmp_path,
    )
    resumed_summary = await resumed.run()

    assert resumed_summary["states"] == {"VERIFIED": 1}
    assert client.writes == 1


@pytest.mark.asyncio
async def test_canary_dls_mismatch_blocks_before_write(tmp_path: Path):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)

    async def wrong_count(*, index: str, body: dict[str, Any]) -> dict[str, int]:
        del index, body
        return {"count": 1}

    client.count = wrong_count  # type: ignore[method-assign]
    canary = _canary(tmp_path, client, record, name="dls")

    summary = await canary.run()

    assert summary["states"] == {"DLS_BLOCKED": 1}
    assert client.writes == 0


@pytest.mark.asyncio
async def test_canary_hash_mismatch_fails_closed_before_write(tmp_path: Path):
    record, directory = _record_and_archive(tmp_path)
    (directory / "original.txt").write_bytes(b"different bytes")
    client = FakeOpenSearch(record)
    canary = _canary(tmp_path, client, record, name="hash-mismatch")

    summary = await canary.run()

    assert summary["states"] == {"FAILED": 1}
    assert client.writes == 0
    assert "hash_mismatch" in canary.checkpoint.data["items"][record.storage_id]["reason"]


@pytest.mark.asyncio
async def test_canary_archive_read_failure_is_explicit_and_retry_safe(tmp_path: Path):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)

    class FailingResolver:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> None:
            raise MetadataExtractionError("archive interrupted")

    checkpoint = CanaryCheckpoint(tmp_path / "archive-failure.json")
    checkpoint.initialize(index_name="documents", records=[record], run_id="archive-failure")
    canary = DocumentMetadataCanary(
        client,
        index_name="documents",
        checkpoint=checkpoint,
        local_archive_root=tmp_path,
        resolver=cast(ArchivedOriginalResolver, FailingResolver()),
    )

    summary = await canary.run()

    assert summary["states"] == {"ARCHIVE_UNAVAILABLE": 1}
    assert client.writes == 0


@pytest.mark.asyncio
async def test_canary_rollback_restores_exact_absence_and_immutable_digest(tmp_path: Path):
    record, _directory = _record_and_archive(tmp_path)
    client = FakeOpenSearch(record)
    canary = _canary(tmp_path, client, record, name="rollback")
    await canary.run()

    result = await canary.rollback([record.storage_id])

    assert result == {"restored": 1, "failed": 0}
    assert all(field not in client.documents[record.storage_id] for field in METADATA_FIELDS)
    item = canary.checkpoint.data["items"][record.storage_id]
    assert item["state"] == CanaryStatus.ROLLED_BACK.value
    assert item["rollback_immutable"] == item["pre_immutable"]
