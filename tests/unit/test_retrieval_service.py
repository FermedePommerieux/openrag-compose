import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.retrieval_service import (
    DiscoveryQuery,
    RetrievalSettings,
    ScopeExhaustiveSettings,
    adaptive_chunk_limit,
    decode_exhaustive_cursor,
    encode_exhaustive_cursor,
    exhaustive_scope_sha256,
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


def test_rrf_same_identity_twice_in_lexical_lane_contributes_once():
    first = _hit("shared", "document")
    duplicate = _hit("shared", "document", text="duplicate serialization")

    fused = reciprocal_rank_fusion([[first, duplicate], [_hit("other", "other")]], k=60)

    by_id = {hit["_id"]: hit for hit in fused}
    assert by_id["shared"]["_retrieval_fusion_score"] == pytest.approx(1 / 61)
    assert by_id["shared"]["_source"]["text"] == "text"


def test_rrf_same_identity_twice_in_dense_lane_contributes_once():
    dense = [_hit("shared", "document"), _hit("shared", "document")]

    fused = reciprocal_rank_fusion([[], dense], k=60)

    assert [hit["_id"] for hit in fused] == ["shared"]
    assert fused[0]["_retrieval_fusion_score"] == pytest.approx(1 / 61)


def test_rrf_same_identity_in_lexical_and_dense_contributes_once_per_lane():
    shared = _hit("shared", "document")

    fused = reciprocal_rank_fusion([[shared, shared], [shared, shared]], k=60)

    assert [hit["_id"] for hit in fused] == ["shared"]
    assert fused[0]["_retrieval_fusion_score"] == pytest.approx(2 / 61)


def test_rrf_duplicate_serialization_variations_preserve_scores_and_order():
    canonical = _hit("shared", "document", text="canonical")
    alternate = _hit("shared", "document", text="alternate")
    other = _hit("other", "other")

    first = reciprocal_rank_fusion([[canonical, other, alternate]], k=60)
    second = reciprocal_rank_fusion([[alternate, other, canonical]], k=60)

    assert [hit["_id"] for hit in first] == ["shared", "other"]
    assert [hit["_id"] for hit in second] == ["shared", "other"]
    assert [hit["_retrieval_fusion_score"] for hit in first] == pytest.approx(
        [hit["_retrieval_fusion_score"] for hit in second]
    )


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


def test_rrf_uses_persisted_chunk_id_across_simulated_shard_ties():
    """A new index stores the same sortable identity independently of shard order."""
    shard_a = [{"_id": "physical-a", "_source": {"chunk_id": "logical-a", "document_id": "a"}}]
    shard_b = [{"_id": "physical-b", "_source": {"chunk_id": "logical-b", "document_id": "b"}}]

    sequences = []
    for iteration in range(20):
        lanes = [shard_b, shard_a] if iteration % 2 else [shard_a, shard_b]
        sequences.append(
            [hit["_source"]["chunk_id"] for hit in reciprocal_rank_fusion(lanes, k=60)]
        )

    assert all(sequence == ["logical-a", "logical-b"] for sequence in sequences)


def test_rrf_legacy_hit_without_sortable_chunk_id_has_explicit_fallback_identity():
    legacy = _hit("legacy-physical-id", "legacy-document")
    current = {
        "_id": "physical-current",
        "_source": {"chunk_id": "current", "document_id": "current"},
    }

    fused = reciprocal_rank_fusion([[current], [legacy]], k=60)

    assert [hit.get("_id") for hit in fused] == ["physical-current", "legacy-physical-id"]


def test_document_diversity_keeps_rank_order_and_caps_each_document():
    hits = [_hit("a1", "a"), _hit("a2", "a"), _hit("b1", "b"), _hit("a3", "a")]

    selected = limit_chunks_per_document(hits, max_chunks_per_document=2)

    assert [hit["_id"] for hit in selected] == ["a1", "a2", "b1"]


def test_adaptive_quota_scales_without_turning_focused_search_into_full_scan():
    assert (
        adaptive_chunk_limit(3, base_chunks_per_document=3, adaptive_max_chunks_per_document=20)
        == 3
    )
    assert (
        adaptive_chunk_limit(100, base_chunks_per_document=3, adaptive_max_chunks_per_document=20)
        == 10
    )
    assert (
        adaptive_chunk_limit(400, base_chunks_per_document=3, adaptive_max_chunks_per_document=20)
        == 20
    )
    assert (
        adaptive_chunk_limit(None, base_chunks_per_document=3, adaptive_max_chunks_per_document=20)
        == 3
    )


def test_adaptive_diversity_gives_each_document_base_quota_before_extra_fill():
    hits = []
    for identifier, document_id in [
        ("a1", "a"),
        ("a2", "a"),
        ("a3", "a"),
        ("b1", "b"),
        ("a4", "a"),
        ("b2", "b"),
    ]:
        hit = _hit(identifier, document_id)
        hit["_source"]["document_chunk_count"] = 100
        hits.append(hit)

    selected = limit_chunks_per_document(
        hits,
        max_chunks_per_document=2,
        adaptive_max_chunks_per_document=10,
    )

    assert [hit["_id"] for hit in selected] == ["a1", "a2", "b1", "b2", "a3", "a4"]


def test_exhaustive_cursor_is_snapshot_and_document_bound():
    scope = exhaustive_scope_sha256(user_id="user-1", filters={"owners": ["user-1"]})
    cursor = encode_exhaustive_cursor(
        document_id="document-a",
        snapshot_sha256="a" * 64,
        search_after=[4, 5, "chunk-5"],
        covered_chunks=5,
        scope_sha256=scope,
    )

    assert decode_exhaustive_cursor(
        cursor,
        document_id="document-a",
        scope_sha256=scope,
    ) == {
        "v": 1,
        "document_id": "document-a",
        "snapshot_sha256": "a" * 64,
        "search_after": [4, 5, "chunk-5"],
        "covered_chunks": 5,
        "scope_sha256": scope,
    }
    with pytest.raises(ValueError, match="another document"):
        decode_exhaustive_cursor(cursor, document_id="document-b", scope_sha256=scope)

    other_scope = exhaustive_scope_sha256(user_id="user-2", filters={})
    with pytest.raises(ValueError, match="another access scope"):
        decode_exhaustive_cursor(
            cursor,
            document_id="document-a",
            scope_sha256=other_scope,
        )


def test_exhaustive_cursor_rejects_tampered_coverage_accounting():
    scope = exhaustive_scope_sha256(user_id="user-1", filters={})
    cursor = encode_exhaustive_cursor(
        document_id="document-a",
        snapshot_sha256="a" * 64,
        search_after=[4, 5, "chunk-5"],
        covered_chunks=5,
        scope_sha256=scope,
    )
    encoded, signature = cursor.split(".", 1)
    tampered = ("A" if encoded[0] != "A" else "B") + encoded[1:] + "." + signature
    with pytest.raises(ValueError, match="Invalid exhaustive retrieval cursor"):
        decode_exhaustive_cursor(
            tampered,
            document_id="document-a",
            scope_sha256=scope,
        )


@pytest.mark.asyncio
async def test_exhaustive_read_paginates_one_immutable_snapshot(monkeypatch):
    from services import search_service

    snapshot = "b" * 64

    def evidence_hit(index: int) -> dict:
        text = f"page {index + 1}"
        return {
            "_id": f"physical-{index}",
            "sort": [index, index + 1, f"logical-{index}"],
            "_source": {
                "chunk_id": f"logical-{index}",
                "chunk_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "document_id": "document-1",
                "document_content_sha256": snapshot,
                "document_order_verified": True,
                "document_chunk_count": 3,
                "filename": "large.pdf",
                "page": index + 1,
                "chunk_index": index,
                "text": text,
                "source_provenance": {
                    "schema_version": "1.0",
                    "entity": {
                        "id": "urn:openrag:document:large",
                        "type": "document",
                    },
                },
                "source_entity_id": "urn:openrag:document:large",
                "source_relation_roles": ["contained_in"],
            },
        }

    class OpenSearchClient:
        bodies: list[dict] = []

        async def search(self, *, index, body, params):
            self.bodies.append(body)
            page = (
                [evidence_hit(2)]
                if body.get("search_after")
                else [evidence_hit(0), evidence_hit(1)]
            )
            return {
                "hits": {"total": {"value": 3, "relation": "eq"}, "hits": page},
                "aggregations": {"snapshots": {"buckets": [{"key": snapshot, "doc_count": 3}]}},
            }

    monkeypatch.setattr(search_service, "get_index_name", lambda: "documents")
    client = OpenSearchClient()
    session_manager = MagicMock()
    session_manager.get_user_opensearch_client.return_value = client
    service = SearchService(session_manager=session_manager)

    first = await service.read_document_chunks(
        "document-1", user_id="user-1", jwt_token="jwt", batch_size=2
    )
    assert first["coverage"]["complete"] is False
    assert first["coverage"]["covered_chunks"] == 2
    assert first["coverage"]["total_chunks"] == 3
    assert first["coverage"]["filename"] == "large.pdf"
    assert [item["evidence_order"] for item in first["results"]] == [1, 2]
    assert first["results"][0]["source_entity_id"] == "urn:openrag:document:large"
    assert first["results"][0]["source_relation_roles"] == ["contained_in"]
    assert "source_provenance" in client.bodies[0]["_source"]

    second = await service.read_document_chunks(
        "document-1",
        user_id="user-1",
        jwt_token="jwt",
        cursor=first["coverage"]["next_cursor"],
        batch_size=2,
    )
    assert second["coverage"]["complete"] is True
    assert second["coverage"]["coverage_ratio"] == 1.0
    assert second["coverage"]["next_cursor"] is None
    assert second["results"][0]["evidence_order"] == 3
    assert {"term": {"document_content_sha256": snapshot}} in client.bodies[1]["query"]["bool"][
        "filter"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"chunk_id": None}, "unverifiable source chunk"),
        ({"chunk_content_sha256": "0" * 64}, "chunk text digest mismatch"),
        ({"chunk_index": 1}, "non-contiguous source order"),
    ],
)
async def test_exhaustive_read_rejects_missing_corrupt_or_noncontiguous_chunk(
    monkeypatch, mutation, message
):
    from services import search_service

    text = "verified text"
    snapshot = "b" * 64
    source = {
        "chunk_id": "logical-0",
        "chunk_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "document_id": "document-1",
        "document_content_sha256": snapshot,
        "document_order_verified": True,
        "document_chunk_count": 1,
        "filename": "document.pdf",
        "chunk_index": 0,
        "text": text,
        **mutation,
    }

    class OpenSearchClient:
        async def search(self, *, index, body, params):
            return {
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [{"_id": "physical-0", "sort": [0], "_source": source}],
                },
                "aggregations": {"snapshots": {"buckets": [{"key": snapshot, "doc_count": 1}]}},
            }

    monkeypatch.setattr(search_service, "get_index_name", lambda: "documents")
    session_manager = MagicMock()
    session_manager.get_user_opensearch_client.return_value = OpenSearchClient()
    service = SearchService(session_manager=session_manager)

    with pytest.raises(RuntimeError, match=message):
        await service.read_document_chunks("document-1", user_id="user-1", jwt_token="jwt")


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

    assert settings.strategy == "rrf"
    assert settings.mode == "hybrid"
    assert settings.lexical_candidates == 500
    assert settings.vector_candidates == 1
    assert settings.rrf_k == 60
    assert settings.max_chunks_per_document == 1
    assert settings.reranker_url == ""
    assert settings.reranker_timeout == 120
    assert settings.debug is True

    scope_settings = ScopeExhaustiveSettings.from_knowledge(
        SimpleNamespace(
            retrieval_scope_seed_count=9999,
            retrieval_scope_max_depth="bad",
            retrieval_scope_max_entities=0,
            retrieval_scope_max_documents=9999,
            retrieval_scope_batch_size=9999,
        )
    )
    assert scope_settings.seed_count == 500
    assert scope_settings.max_depth == 8
    assert scope_settings.max_entities == 1
    assert scope_settings.max_documents == 1000
    assert scope_settings.batch_size == 50


@pytest.mark.asyncio
async def test_search_service_rrf_fuses_lanes_preserves_provenance_and_emits_debug(monkeypatch):
    """RRF is an explicit opt-in and never relies on incomparable raw scores."""
    from services import search_service

    knowledge = SimpleNamespace(
        embedding_provider="openai",
        retrieval_strategy="rrf",
        retrieval_mode="hybrid",
        retrieval_lexical_candidates=5,
        retrieval_vector_candidates=80,
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

        class indices:
            @staticmethod
            async def get_mapping(*, index):
                return {index: {"mappings": {"properties": {"chunk_id": {"type": "keyword"}}}}}

        async def search(self, *, index, body, params):
            self.bodies.append(body)
            if body.get("size") == 0:
                return {
                    "aggregations": {
                        "embedding_models": {"buckets": [{"key": "test-model", "doc_count": 3}]}
                    }
                }
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

    result = await SearchService(session_manager=session_manager).search_tool(
        "shared text",
        _discovery_query=DiscoveryQuery(
            query_id="q1",
            query_text="shared text",
            query_type="conceptual_variant",
            parent_query="original text",
            generation_method="test",
        ),
        _include_timing=True,
    )

    assert [item["chunk_id"] for item in result["results"]] == ["shared", "lexical"]
    assert result["results"][0]["source_url"] == "https://example.test/b"
    assert result["results"][0]["document_id"] == "document-b"
    assert result["results"][0]["connector_file_id"] == "drive-file-b"
    assert result["results"][0]["filename"] == "b.pdf"
    assert result["results"][0]["page"] == 3
    assert result["results"][0]["chunk_index"] == 0
    assert result["results"][0]["chunking_strategy"] == "hybrid"
    assert result["retrieval_debug"]["lanes"] == {"lexical": 2, "vector": 2}
    assert result["results"][0]["matched_queries"] == ["q1"]
    assert result["results"][0]["matched_lanes"] == ["lexical", "dense"]
    assert result["results"][0]["query_contributions"][0]["lexical_rank"] == 2
    assert result["results"][0]["query_contributions"][0]["dense_rank"] == 1
    assert result["_retrieval_timing"]["total_seconds"] >= 0
    lane_bodies = [body for body in OpenSearchClient.bodies if body.get("size") != 0]
    assert len(lane_bodies) == 2
    assert all(
        body["sort"]
        == [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ]
        for body in lane_bodies
    )
    vector_body = next(
        body for body in lane_bodies if "dis_max" in body["query"]["bool"]["should"][0]
    )
    knn = vector_body["query"]["bool"]["should"][0]["dis_max"]["queries"][0]["knn"]
    knn_parameters = next(iter(knn.values()))
    assert vector_body["size"] == 80
    assert knn_parameters["k"] == 80
    assert knn_parameters["num_candidates"] == 1000


@pytest.mark.asyncio
async def test_document_search_paginates_collapsed_results_with_server_total(monkeypatch):
    from services import search_service

    knowledge = SimpleNamespace(
        embedding_provider="openai",
        retrieval_strategy="weighted",
        retrieval_mode="hybrid",
        retrieval_lexical_candidates=500,
        retrieval_vector_candidates=500,
        retrieval_rrf_k=60,
        retrieval_max_chunks_per_document=3,
        retrieval_adaptive_max_chunks_per_document=20,
        retrieval_reranker_url="",
        retrieval_reranker_timeout=5,
        retrieval_debug=False,
    )
    config = SimpleNamespace(
        knowledge=knowledge,
        providers=SimpleNamespace(ollama=SimpleNamespace(endpoint="")),
    )
    monkeypatch.setattr(search_service, "get_openrag_config", lambda: config)
    monkeypatch.setattr(search_service, "get_embedding_model", lambda: "test-model")
    monkeypatch.setattr(search_service, "get_index_name", lambda: "documents")
    monkeypatch.setattr(search_service, "get_auth_context", lambda: ("user-1", "jwt"))

    class OpenSearchClient:
        bodies: list[dict] = []

        async def search(self, *, index, body, params):
            self.bodies.append(body)
            if body.get("size") == 0:
                return {
                    "aggregations": {
                        "embedding_models": {"buckets": [{"key": "test-model", "doc_count": 245}]}
                    }
                }
            return {
                "hits": {
                    "hits": [
                        {
                            "_id": "chunk-a",
                            "_score": 1.0,
                            "_source": {
                                "chunk_id": "chunk-a",
                                "document_id": "document-a",
                                "filename": "a.pdf",
                                "text": "pastoral project",
                            },
                        },
                        {
                            "_id": "chunk-b",
                            "_score": 0.9,
                            "_source": {
                                "chunk_id": "chunk-b",
                                "document_id": "document-b",
                                "filename": "b.pdf",
                                "text": "pastoral reply",
                            },
                        },
                    ]
                },
                "aggregations": {
                    "document_names": {
                        "buckets": [
                            {"key": f"document-{index}.pdf", "doc_count": 1} for index in range(245)
                        ],
                        "sum_other_doc_count": 0,
                    },
                    "data_sources": {"buckets": []},
                },
            }

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
    client = OpenSearchClient()
    session_manager = MagicMock()
    session_manager.get_user_opensearch_client.return_value = client

    result = await SearchService(session_manager=session_manager).search_tool(
        "pastoral",
        group_by_document=True,
        page=2,
        page_size=100,
    )

    assert result["total_documents"] == 245
    assert result["page"] == 2
    assert result["page_size"] == 100
    assert result["total_documents_capped"] is False
    assert "document_names" not in result["aggregations"]
    assert [item["filename"] for item in result["results"]] == ["a.pdf", "b.pdf"]
    search_body = client.bodies[-1]
    assert search_body["from"] == 100
    assert search_body["size"] == 100
    assert search_body["collapse"] == {"field": "filename"}
    assert search_body["aggs"]["document_names"] == {
        "terms": {
            "field": "filename",
            "size": 10_000,
            "shard_size": 10_000,
        }
    }
    knn = search_body["query"]["bool"]["should"][0]["dis_max"]["queries"][0]["knn"][
        "chunk_embedding_test_model"
    ]
    assert knn["k"] == 10_000
    assert knn["num_candidates"] == 10_000
