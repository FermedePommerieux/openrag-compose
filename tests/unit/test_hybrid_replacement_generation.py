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
        "same-content", client, fingerprint=_fingerprint("hybrid")
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
        "same-content", client, fingerprint=fingerprint
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
        "same-content", client, fingerprint=_fingerprint("hybrid", max_tokens=512)
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
