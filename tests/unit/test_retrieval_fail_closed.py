from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.search_service import SearchService


def _hit(chunk_id: str, document_id: str, text: str) -> dict:
    return {
        "_id": chunk_id,
        "_score": 1.0,
        "_source": {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "filename": f"{document_id}.pdf",
            "text": text,
        },
    }


async def _run_rrf_search(monkeypatch, *, mode: str, embed_fails: bool, failed_lane: str = ""):
    from services import search_service

    knowledge = SimpleNamespace(
        embedding_provider="openai",
        retrieval_strategy="rrf",
        retrieval_mode=mode,
        retrieval_lexical_candidates=5,
        retrieval_vector_candidates=5,
        retrieval_rrf_k=60,
        retrieval_max_chunks_per_document=3,
        retrieval_adaptive_max_chunks_per_document=20,
        retrieval_reranker_url="",
        retrieval_reranker_timeout=5,
        retrieval_debug=False,
    )
    monkeypatch.setattr(
        search_service,
        "get_openrag_config",
        lambda: SimpleNamespace(knowledge=knowledge),
    )
    monkeypatch.setattr(search_service, "get_embedding_model", lambda: "test-model")
    monkeypatch.setattr(search_service, "get_index_name", lambda: "documents")
    monkeypatch.setattr(search_service, "get_auth_context", lambda: ("user-1", "jwt"))
    monkeypatch.setattr(search_service.asyncio, "sleep", AsyncMock())

    class OpenSearchClient:
        class indices:
            @staticmethod
            async def get_mapping(*, index):
                return {index: {"mappings": {"properties": {"chunk_id": {"type": "keyword"}}}}}

        async def search(self, *, index, body, params):
            if body.get("size") == 0:
                return {
                    "aggregations": {
                        "embedding_models": {"buckets": [{"key": "test-model", "doc_count": 2}]}
                    }
                }
            should = body.get("query", {}).get("bool", {}).get("should", [])
            lane = "dense" if should and "dis_max" in should[0] else "lexical"
            if failed_lane == lane:
                raise RuntimeError(f"forced {lane} failure")
            hit = _hit(f"{lane}-chunk", f"{lane}-document", f"{lane} evidence")
            return {"hits": {"hits": [hit]}, "aggregations": {}}

    embedding_create = AsyncMock(
        side_effect=(RuntimeError("forced embedding failure") if embed_fails else None),
        return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])]),
    )
    monkeypatch.setattr(
        search_service,
        "clients",
        SimpleNamespace(
            patched_embedding_client=SimpleNamespace(
                embeddings=SimpleNamespace(create=embedding_create)
            )
        ),
    )
    session_manager = MagicMock()
    session_manager.get_user_opensearch_client.return_value = OpenSearchClient()
    return await SearchService(session_manager=session_manager).search_tool("contract Alpha")


@pytest.mark.asyncio
async def test_hybrid_embedding_failure_preserves_lexical_results_but_fails_closed(monkeypatch):
    result = await _run_rrf_search(monkeypatch, mode="hybrid", embed_fails=True)

    assert result["results"]
    assert result["effective_retrieval_profile"]["mode"] == "lexical"
    assert result["effective_retrieval_profile"]["lanes"]["lexical"]["status"] == "succeeded"
    assert result["effective_retrieval_profile"]["lanes"]["dense"]["status"] == "failed"
    assert result["effective_retrieval_profile"]["lanes"]["fusion"]["status"] == "failed"
    assert result["retrieval_execution_complete"] is False
    assert "retrieval_dense_lane_failed" in result["retrieval_failure_codes"]


@pytest.mark.asyncio
async def test_vector_embedding_failure_exposes_lexical_fallback_and_fails_closed(monkeypatch):
    result = await _run_rrf_search(monkeypatch, mode="vector", embed_fails=True)

    assert result["results"]
    assert result["requested_retrieval_profile"]["lanes"]["lexical"] == "disabled"
    assert result["effective_retrieval_profile"]["mode"] == "lexical"
    assert result["effective_retrieval_profile"]["lanes"]["lexical"]["requested"] is False
    assert result["effective_retrieval_profile"]["lanes"]["dense"]["status"] == "failed"
    assert result["retrieval_execution_complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_lane", "effective_mode", "failure_code"),
    [
        ("dense", "lexical", "retrieval_dense_lane_failed"),
        ("lexical", "vector", "retrieval_lexical_lane_failed"),
    ],
)
async def test_hybrid_lane_failure_preserves_other_lane_but_fails_closed(
    monkeypatch, failed_lane, effective_mode, failure_code
):
    result = await _run_rrf_search(
        monkeypatch,
        mode="hybrid",
        embed_fails=False,
        failed_lane=failed_lane,
    )

    assert result["results"]
    assert result["effective_retrieval_profile"]["mode"] == effective_mode
    assert result["retrieval_execution_complete"] is False
    assert failure_code in result["retrieval_failure_codes"]


@pytest.mark.asyncio
async def test_hybrid_success_executes_both_lanes_and_fusion(monkeypatch):
    result = await _run_rrf_search(monkeypatch, mode="hybrid", embed_fails=False)

    lanes = result["effective_retrieval_profile"]["lanes"]
    assert result["effective_retrieval_profile"]["mode"] == "hybrid"
    assert lanes["lexical"]["status"] == "succeeded"
    assert lanes["dense"]["status"] == "succeeded"
    assert lanes["fusion"]["status"] == "succeeded"
    assert result["retrieval_execution_complete"] is True
    assert result["retrieval_failure_codes"] == []


@pytest.mark.asyncio
async def test_fusion_failure_preserves_unfused_results_but_fails_closed(monkeypatch):
    from services import search_service

    monkeypatch.setattr(
        search_service,
        "reciprocal_rank_fusion",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced fusion failure")),
    )

    result = await _run_rrf_search(monkeypatch, mode="hybrid", embed_fails=False)

    assert result["results"]
    assert result["effective_retrieval_profile"]["lanes"]["fusion"]["status"] == "failed"
    assert result["retrieval_execution_complete"] is False
    assert "retrieval_fusion_failed" in result["retrieval_failure_codes"]
