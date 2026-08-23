from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.retrieval_service import (
    RetrievalSettings,
    limit_chunks_per_document,
    reciprocal_rank_fusion,
)
from services.search_service import SearchService


def _hit(identifier: str, document_id: str, text: str = "text") -> dict:
    return {"_id": identifier, "_source": {"document_id": document_id, "text": text}}


def test_rrf_rewards_hits_present_in_both_ranked_lists():
    lexical = [_hit("lexical-only", "a"), _hit("shared", "b")]
    vector = [_hit("shared", "b"), _hit("vector-only", "c")]

    fused = reciprocal_rank_fusion([lexical, vector], k=60)

    assert [hit["_id"] for hit in fused] == ["shared", "lexical-only", "vector-only"]
    assert fused[0]["_retrieval_fusion_score"] > fused[1]["_retrieval_fusion_score"]
    assert "_retrieval_fusion_score" not in lexical[1]


def test_rrf_is_deterministic_for_equal_scores():
    first = reciprocal_rank_fusion([[_hit("a", "a")], [_hit("b", "b")]], k=60)
    second = reciprocal_rank_fusion([[_hit("a", "a")], [_hit("b", "b")]], k=60)

    assert [hit["_id"] for hit in first] == ["a", "b"]
    assert [hit["_id"] for hit in second] == ["a", "b"]


def test_rrf_tie_break_is_independent_of_lane_response_order():
    """Equal fused scores use a persistent chunk identity, never first_seen."""
    a = _hit("a", "a")
    b = _hit("b", "b")

    first = reciprocal_rank_fusion([[a], [b]], k=60)
    reversed_responses = reciprocal_rank_fusion([[b], [a]], k=60)

    assert [hit["_id"] for hit in first] == ["a", "b"]
    assert [hit["_id"] for hit in reversed_responses] == ["a", "b"]


def test_rrf_is_reproducible_across_twenty_reordered_equal_score_responses():
    """The final chunk sequence must not vary when OpenSearch shuffles ties."""
    expected = ("a", "b")
    sequences = []
    for iteration in range(20):
        lexical = [_hit("a", "a"), _hit("b", "b")]
        vector = list(reversed(lexical)) if iteration % 2 else lexical
        sequences.append(tuple(hit["_id"] for hit in reciprocal_rank_fusion([lexical, vector])))

    assert all(sequence == expected for sequence in sequences)


def test_document_diversity_keeps_rank_order_and_caps_each_document():
    hits = [_hit("a1", "a"), _hit("a2", "a"), _hit("b1", "b"), _hit("a3", "a")]

    selected = limit_chunks_per_document(hits, max_chunks_per_document=2)

    assert [hit["_id"] for hit in selected] == ["a1", "a2", "b1"]


def test_settings_normalize_invalid_or_unbounded_values():
    knowledge = SimpleNamespace(
        retrieval_strategy="unexpected",
        retrieval_mode="unexpected",
        retrieval_lexical_candidates=9999,
        retrieval_vector_candidates=0,
        retrieval_rrf_k="bad",
        retrieval_max_chunks_per_document=-2,
        retrieval_reranker_url=None,
        retrieval_reranker_timeout=999,
        retrieval_debug=True,
    )

    settings = RetrievalSettings.from_knowledge(knowledge)

    assert settings.strategy == "weighted"
    assert settings.mode == "hybrid"
    assert settings.lexical_candidates == 500
    assert settings.vector_candidates == 1
    assert settings.rrf_k == 60
    assert settings.max_chunks_per_document == 1
    assert settings.reranker_url == ""
    assert settings.reranker_timeout == 120
    assert settings.debug is True


@pytest.mark.asyncio
async def test_search_service_rrf_fuses_lanes_preserves_provenance_and_emits_debug(monkeypatch):
    """RRF is an explicit opt-in and never relies on incomparable raw scores."""
    from services import search_service

    knowledge = SimpleNamespace(
        embedding_provider="openai",
        retrieval_strategy="rrf",
        retrieval_mode="hybrid",
        retrieval_lexical_candidates=5,
        retrieval_vector_candidates=5,
        retrieval_rrf_k=60,
        retrieval_max_chunks_per_document=1,
        retrieval_reranker_url="",
        retrieval_reranker_timeout=5,
        retrieval_debug=True,
    )
    config = SimpleNamespace(
        knowledge=knowledge,
        providers=SimpleNamespace(ollama=SimpleNamespace(endpoint="")),
    )
    monkeypatch.setattr(search_service, "get_openrag_config", lambda: config)
    monkeypatch.setattr(search_service, "get_embedding_model", lambda: "test-model")
    monkeypatch.setattr(search_service, "get_index_name", lambda: "documents")
    monkeypatch.setattr(search_service, "get_auth_context", lambda: ("user-1", "jwt"))

    shared = _hit("shared", "document-b", "shared text")
    shared["_source"].update(
        {
            "filename": "b.pdf",
            "source_url": "https://example.test/b",
            "connector_file_id": "drive-file-b",
            "page": 3,
            "chunk_index": 0,
            "chunking_strategy": "hybrid",
        }
    )
    lexical_only = _hit("lexical", "document-a", "lexical text")
    lexical_only["_source"].update({"filename": "a.pdf", "chunk_index": 2})
    vector_same_document = _hit("vector-a", "document-a", "other text")

    class OpenSearchClient:
        bodies: list[dict] = []

        async def search(self, *, index, body, params):
            self.bodies.append(body)
            if body.get("size") == 0:
                return {"aggregations": {"embedding_models": {"buckets": [{"key": "test-model", "doc_count": 3}]}}}
            should = body.get("query", {}).get("bool", {}).get("should", [])
            is_vector = bool(should and "dis_max" in should[0])
            hits = [shared, vector_same_document] if is_vector else [lexical_only, shared]
            return {"hits": {"hits": hits}, "aggregations": {"data_sources": {"buckets": []}}}

    embedding_response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])
    monkeypatch.setattr(
        search_service,
        "clients",
        SimpleNamespace(
            patched_embedding_client=SimpleNamespace(
                embeddings=SimpleNamespace(create=AsyncMock(return_value=embedding_response))
            )
        ),
    )
    session_manager = MagicMock()
    session_manager.get_user_opensearch_client.return_value = OpenSearchClient()

    result = await SearchService(session_manager=session_manager).search_tool("shared text")

    assert [item["chunk_id"] for item in result["results"]] == ["shared", "lexical"]
    assert result["results"][0]["source_url"] == "https://example.test/b"
    assert result["results"][0]["document_id"] == "document-b"
    assert result["results"][0]["connector_file_id"] == "drive-file-b"
    assert result["results"][0]["filename"] == "b.pdf"
    assert result["results"][0]["page"] == 3
    assert result["results"][0]["chunk_index"] == 0
    assert result["results"][0]["chunking_strategy"] == "hybrid"
    assert result["retrieval_debug"]["lanes"] == {"lexical": 2, "vector": 2}
    lane_bodies = [body for body in OpenSearchClient.bodies if body.get("size") != 0]
    assert len(lane_bodies) == 2
    assert all(body["sort"] == [{"_score": {"order": "desc"}}, {"_id": {"order": "asc"}}] for body in lane_bodies)
