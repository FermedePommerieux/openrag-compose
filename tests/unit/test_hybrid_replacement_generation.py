"""Hybrid replacement invariants for the two-phase backend path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.processors import TaskProcessor


def _fingerprint(strategy: str, *, max_tokens: int = 512, merge_peers: bool = True) -> str:
    return TaskProcessor._chunking_config_fingerprint(
        strategy=strategy,
        chunk_size=1000,
        chunk_overlap=200,
        hybrid_max_tokens=max_tokens,
        hybrid_merge_peers=merge_peers,
    )


@pytest.mark.asyncio
async def test_character_existing_to_hybrid_is_not_unchanged():
    client = AsyncMock()
    client.search.return_value = {
        "hits": {"hits": [{"_source": {"chunking_config_fingerprint": _fingerprint("character")}}]}
    }

    matches = await TaskProcessor().check_document_matches_chunking(
        "same-content", client, fingerprint=_fingerprint("hybrid"), owner_user_id="owner", shared=False
    )

    assert matches is False


@pytest.mark.asyncio
async def test_hybrid_existing_with_same_parameters_is_unchanged():
    fingerprint = _fingerprint("hybrid")
    client = AsyncMock()
    client.search.return_value = {
        "hits": {"hits": [{"_source": {"chunking_config_fingerprint": fingerprint}}]}
    }

    assert await TaskProcessor().check_document_matches_chunking(
        "same-content", client, fingerprint=fingerprint, owner_user_id="owner", shared=False
    )


@pytest.mark.asyncio
async def test_hybrid_existing_with_different_parameters_reindexes():
    client = AsyncMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {"_source": {"chunking_config_fingerprint": _fingerprint("hybrid", max_tokens=256)}}
            ]
        }
    }

    assert not await TaskProcessor().check_document_matches_chunking(
        "same-content",
        client,
        fingerprint=_fingerprint("hybrid", max_tokens=512),
        owner_user_id="owner",
        shared=False,
    )


@pytest.mark.asyncio
async def test_hybrid_failure_does_not_delete_character_generation(tmp_path, monkeypatch):
    """An explicit Hybrid failure occurs before any promotion/deletion call."""
    from models import processors as processors_mod

    user_client = AsyncMock()
    user_client.search.return_value = {
        "hits": {"hits": [{"_source": {"chunking_config_fingerprint": _fingerprint("character")}}]}
    }
    session = MagicMock()
    session.get_user_opensearch_client.return_value = user_client
    service = SimpleNamespace(session_manager=session, document_index_writer=MagicMock())
    processor = TaskProcessor(service, models_service=MagicMock(), docling_service=MagicMock())
    config = SimpleNamespace(
        knowledge=SimpleNamespace(
            embedding_model="model",
            chunking_strategy="hybrid",
            chunk_size=1000,
            chunk_overlap=200,
            hybrid_max_tokens=512,
            hybrid_merge_peers=True,
        )
    )
    monkeypatch.setattr(processors_mod, "get_openrag_config", lambda: config)

    path = tmp_path / "existing.txt"
    path.write_text("old content")
    with pytest.raises(ValueError, match="requested_chunking_strategy=hybrid"):
        await processor.process_document_standard(str(path), "same-content", owner_user_id="owner")

    user_client.delete.assert_not_called()
    service.document_index_writer.index_chunks.assert_not_called()


@pytest.mark.asyncio
async def test_rename_promotion_deletes_old_connector_chunks_only_after_success(monkeypatch):
    """The connector's old filename generation is deleted in promotion phase two."""
    from models import processors as processors_mod

    old_id = "old-connector-generation"
    new_id = "new-temporary-generation"
    calls: list[str] = []

    class Client:
        async def search(self, **kwargs):
            calls.append("search")
            return {"_scroll_id": None, "hits": {"hits": [{"_id": old_id}, {"_id": new_id}]}}

        async def delete(self, **kwargs):
            calls.append("delete")
            assert kwargs["id"] == old_id
            return {"result": "deleted"}

    monkeypatch.setattr(processors_mod, "get_index_name", lambda: "documents")
    monkeypatch.setattr(processors_mod, "clients", SimpleNamespace(opensearch=Client()))

    await TaskProcessor()._promote_document_generation(
        opensearch_client=Client(),
        new_storage_ids={new_id},
        file_hash="new-hash",
        filename="renamed.pdf",
        owner_user_id="owner",
        shared=False,
        replace_existing_filename=False,
        connector_file_id="connector-1",
        connector_type="sharepoint",
    )

    assert calls[-1] == "delete"


@pytest.mark.asyncio
async def test_rename_promotion_failure_leaves_old_connector_chunks_untouched(monkeypatch):
    from models import processors as processors_mod

    class Client:
        async def search(self, **kwargs):
            raise RuntimeError("OpenSearch unavailable")

        async def delete(self, **kwargs):
            raise AssertionError("old chunks must not be deleted")

    monkeypatch.setattr(processors_mod, "get_index_name", lambda: "documents")
    monkeypatch.setattr(processors_mod, "clients", SimpleNamespace(opensearch=Client()))

    with pytest.raises(RuntimeError, match="OpenSearch unavailable"):
        await TaskProcessor()._promote_document_generation(
            opensearch_client=Client(),
            new_storage_ids={"temporary"},
            file_hash="new-hash",
            filename="renamed.pdf",
            owner_user_id="owner",
            shared=False,
            replace_existing_filename=False,
            connector_file_id="connector-1",
            connector_type="sharepoint",
        )


@pytest.mark.asyncio
async def test_hybrid_promotion_same_hash_never_selects_another_owner(monkeypatch):
    """The administrative delete path must scope same-byte documents by owner."""
    from models import processors as processors_mod

    selected_queries: list[dict] = []
    deleted_ids: list[str] = []

    class UserClient:
        async def search(self, *, body, **kwargs):
            selected_queries.append(body["query"])
            return {"_scroll_id": None, "hits": {"hits": [{"_id": "owner-a-old", "_source": {"owner": "A"}}]}}

    class WriteClient:
        async def delete(self, **kwargs):
            deleted_ids.append(kwargs["id"])
            return {"result": "deleted"}

    monkeypatch.setattr(processors_mod, "get_index_name", lambda: "documents")
    monkeypatch.setattr(processors_mod, "clients", SimpleNamespace(opensearch=WriteClient()))

    await TaskProcessor()._promote_document_generation(
        opensearch_client=UserClient(),
        new_storage_ids={"owner-a-new"},
        file_hash="identical-content-hash",
        filename="same.pdf",
        owner_user_id="A",
        shared=False,
        replace_existing_filename=False,
        connector_file_id=None,
        connector_type="local",
    )

    assert selected_queries == [
        {
            "bool": {
                "filter": [
                    {"term": {"document_id": "identical-content-hash"}},
                    {"term": {"owner": "A"}},
                ]
            }
        }
    ]
    assert deleted_ids == ["owner-a-old"]
    assert "owner-b-old" not in deleted_ids


@pytest.mark.asyncio
async def test_hybrid_partial_delete_restores_every_snapshot_chunk(monkeypatch):
    """A deletion error restores the whole prior generation and then fails."""
    from models import processors as processors_mod

    class UserClient:
        async def search(self, **kwargs):
            return {
                "_scroll_id": None,
                "hits": {
                    "hits": [
                        {"_id": "old-1", "_source": {"document_id": "hash", "owner": "A"}},
                        {"_id": "old-2", "_source": {"document_id": "hash", "owner": "A"}},
                    ]
                },
            }

    class WriteClient:
        def __init__(self):
            self.deleted = 0
            self.bulk_calls: list[dict] = []

        async def delete(self, **kwargs):
            self.deleted += 1
            if self.deleted == 2:
                raise RuntimeError("delete second chunk failed")
            return {"result": "deleted"}

        async def bulk(self, **kwargs):
            self.bulk_calls.append(kwargs)
            return {
                "errors": False,
                "items": [
                    {"index": {"_id": "old-1", "status": 201}},
                    {"index": {"_id": "old-2", "status": 201}},
                ],
            }

    write_client = WriteClient()
    monkeypatch.setattr(processors_mod, "get_index_name", lambda: "documents")
    monkeypatch.setattr(processors_mod, "clients", SimpleNamespace(opensearch=write_client))

    with pytest.raises(RuntimeError, match="delete second chunk failed"):
        await TaskProcessor()._promote_document_generation(
            opensearch_client=UserClient(),
            new_storage_ids={"new"},
            file_hash="hash",
            filename="same.pdf",
            owner_user_id="A",
            shared=False,
            replace_existing_filename=False,
            connector_file_id=None,
            connector_type="local",
        )

    assert len(write_client.bulk_calls) == 1
    restore_body = write_client.bulk_calls[0]["body"]
    assert [restore_body[index]["index"]["_id"] for index in range(0, len(restore_body), 2)] == [
        "old-1",
        "old-2",
    ]


@pytest.mark.asyncio
async def test_hybrid_partial_rollback_is_fatal_when_bulk_item_fails(monkeypatch):
    """A HTTP-successful bulk response with a failed item is not a rollback."""
    from models import processors as processors_mod

    class UserClient:
        async def search(self, **kwargs):
            return {
                "_scroll_id": None,
                "hits": {"hits": [{"_id": "old", "_source": {"document_id": "hash", "owner": "A"}}]},
            }

    class WriteClient:
        async def delete(self, **kwargs):
            raise RuntimeError("delete failed")

        async def bulk(self, **kwargs):
            return {
                "errors": True,
                "items": [
                    {"index": {"_id": "old", "status": 429, "error": {"type": "rejected_execution"}}}
                ],
            }

    monkeypatch.setattr(processors_mod, "get_index_name", lambda: "documents")
    monkeypatch.setattr(processors_mod, "clients", SimpleNamespace(opensearch=WriteClient()))

    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        await TaskProcessor()._promote_document_generation(
            opensearch_client=UserClient(),
            new_storage_ids={"new"},
            file_hash="hash",
            filename="same.pdf",
            owner_user_id="A",
            shared=False,
            replace_existing_filename=False,
            connector_file_id=None,
            connector_type="local",
        )
