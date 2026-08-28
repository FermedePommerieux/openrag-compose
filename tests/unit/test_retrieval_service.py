import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.retrieval_service import (
    RetrievalSettings,
    adaptive_chunk_limit,
    consensus_rank_fusion,
    decode_exhaustive_cursor,
    encode_exhaustive_cursor,
    exhaustive_scope_sha256,
    limit_chunks_per_document,
    normalized_score_fusion,
    reciprocal_rank_fusion,
)
from services.search_service import (
    PROVENANCE_TRAVERSAL_PREDICATES,
    SearchService,
    _build_document_graph,
    _propagate_provenance_paths,
    _provenance_relation_paths,
    _provenance_relation_target_query,
)


def _hit(identifier: str, document_id: str, text: str = "text") -> dict:
    return {"_id": identifier, "_source": {"document_id": document_id, "text": text}}


def test_document_graph_keeps_relation_noise_separate_without_excluding_it():
    direct_hit = _hit("direct-chunk", "direct")
    context_hit = _hit("context-chunk", "context")
    context_hit["_source"].update(
        {
            "retrieval_relation_depth": 1,
            "retrieval_relation_paths": [
                {
                    "from_document_id": "direct",
                    "to_document_id": "context",
                    "relation_role": "reply_to",
                    "direction": "anchor_to_candidate",
                    "via_entity_id": "urn:mail:context",
                }
            ],
        }
    )
    retrieval_results = {
        "lexical": {"hits": {"hits": [direct_hit]}},
        "provenance": {"hits": {"hits": [context_hit]}},
    }
    chunks = [
        {"document_id": "direct", "chunk_id": "direct-chunk", "filename": "direct.eml"},
        {
            "document_id": "context",
            "chunk_id": "context-chunk",
            "filename": "context.eml",
            "retrieval_relation_depth": 1,
            "retrieval_relation_paths": context_hit["_source"]["retrieval_relation_paths"],
        },
    ]

    register, graph = _build_document_graph(chunks, retrieval_results)

    assert [item["retrieval_plane"] for item in register] == ["direct", "context"]
    assert register[1]["relevance"]["level"] == "contextual"
    assert register[1]["relevance"]["probability_calibrated"] is False
    assert graph["llm_generated"] is False
    assert graph["edge_count"] == 1
    assert graph["edges"][0]["prov_predicate"] == ("http://www.w3.org/ns/prov#wasInfluencedBy")
    assert graph["canonical_relation_field"] == ("source_provenance.relations.prov_predicate")


def test_provenance_query_uses_canonical_predicates_and_bounded_legacy_fallback():
    query = _provenance_relation_target_query({"urn:mail:anchor"})
    nested_bool = query["nested"]["query"]["bool"]

    assert nested_bool["filter"] == [
        {"terms": {"source_provenance.relations.target.id": ["urn:mail:anchor"]}}
    ]
    assert nested_bool["should"][0] == {
        "terms": {
            "source_provenance.relations.prov_predicate": sorted(PROVENANCE_TRAVERSAL_PREDICATES)
        }
    }
    assert nested_bool["must_not"] == [
        {"terms": {"source_provenance.relations.role": ["contained_in"]}}
    ]
    assert nested_bool["should"][1]["bool"]["must_not"] == [
        {"exists": {"field": "source_provenance.relations.prov_predicate"}}
    ]


def test_provenance_path_resolves_implicit_reply_to_explicit_anchor():
    anchor = _hit("anchor-chunk", "anchor", "Anciennes surfaces pastorales")
    anchor["_source"].update(
        {
            "filename": "explicit-request.eml",
            "source_entity_id": "urn:mail:anchor",
        }
    )
    reply = _hit("reply-chunk", "reply", "Nous soutenons votre projet")
    reply["_source"].update(
        {
            "source_entity_id": "urn:mail:implicit-reply",
            "source_provenance": {
                "relations": [
                    {
                        "role": "reply_to",
                        "target": {"id": "urn:mail:anchor"},
                    }
                ]
            },
        }
    )

    paths = _provenance_relation_paths({"anchor": anchor}, reply)

    assert paths == [
        {
            "from_document_id": "anchor",
            "from_filename": "explicit-request.eml",
            "to_document_id": "reply",
            "prov_predicate": "http://www.w3.org/ns/prov#wasInfluencedBy",
            "relation_role": "reply_to",
            "direction": "candidate_to_anchor",
            "via_entity_id": "urn:mail:anchor",
            "anchor_excerpt": "Anciennes surfaces pastorales",
        }
    ]


def test_shared_target_graph_projection_preserves_both_prov_assertions():
    anchor = _hit("anchor-chunk", "anchor")
    anchor["_source"].update(
        {
            "source_entity_id": "urn:mail:anchor",
            "source_provenance": {
                "relations": [
                    {
                        "role": "member_of",
                        "target": {"id": "urn:thread:one"},
                    }
                ]
            },
        }
    )
    candidate = _hit("candidate-chunk", "candidate")
    candidate["_source"].update(
        {
            "source_entity_id": "urn:mail:candidate",
            "source_provenance": {
                "relations": [
                    {
                        "role": "member_of",
                        "target": {"id": "urn:thread:one"},
                    }
                ]
            },
        }
    )
    paths = _provenance_relation_paths({"anchor": anchor}, candidate)
    candidate["_source"]["retrieval_relation_paths"] = paths
    candidate["_source"]["retrieval_relation_depth"] = 1

    _register, graph = _build_document_graph(
        [
            {"document_id": "anchor", "chunk_id": "anchor-chunk"},
            {
                "document_id": "candidate",
                "chunk_id": "candidate-chunk",
                "retrieval_relation_paths": paths,
                "retrieval_relation_depth": 1,
            },
        ],
        {
            "lexical": {"hits": {"hits": [anchor]}},
            "provenance": {"hits": {"hits": [candidate]}},
        },
    )

    assert graph["edges"] == [
        {
            "source_document_id": "anchor",
            "target_document_id": "candidate",
            "prov_predicate": None,
            "role": None,
            "via_entity_id": "urn:thread:one",
            "evidence": "retrieval_relation_path",
            "edge_kind": "shared_target_projection",
            "assertions": [
                {
                    "document_id": "anchor",
                    "prov_predicate": "http://www.w3.org/ns/prov#wasMemberOf",
                    "role": "member_of",
                    "target_entity_id": "urn:thread:one",
                },
                {
                    "document_id": "candidate",
                    "prov_predicate": "http://www.w3.org/ns/prov#wasMemberOf",
                    "role": "member_of",
                    "target_entity_id": "urn:thread:one",
                },
            ],
        }
    ]


def test_provenance_path_survives_when_rrf_keeps_a_vector_copy():
    vector_hit = _hit("vector-chunk", "related", "Nous soutenons votre projet")
    provenance_hit = _hit("provenance-chunk", "related", "Nous soutenons votre projet")
    provenance_hit["_source"]["retrieval_relation_paths"] = [
        {
            "from_document_id": "anchor",
            "to_document_id": "related",
            "relation_role": "reply_to",
        }
    ]
    retrieval_results = {
        "vector:model": {"hits": {"hits": [vector_hit]}},
        "provenance": {"hits": {"hits": [provenance_hit]}},
    }

    _propagate_provenance_paths(retrieval_results, [provenance_hit])

    assert (
        vector_hit["_source"]["retrieval_relation_paths"]
        == provenance_hit["_source"]["retrieval_relation_paths"]
    )


@pytest.mark.asyncio
async def test_normal_search_does_not_start_document_reads_or_llm_synthesis(monkeypatch):
    from services import search_service

    monkeypatch.setattr(search_service, "get_auth_context", lambda: ("user-1", "jwt"))
    service = SearchService(session_manager=MagicMock())
    service.search_tool = AsyncMock(
        return_value={
            "results": [
                {
                    "document_id": "document-a",
                    "chunk_id": "chunk-a",
                    "filename": "a.eml",
                    "retrieval_relation_paths": [
                        {
                            "from_document_id": "document-b",
                            "relation_role": "reply_to",
                        }
                    ],
                },
                {"document_id": "document-b", "chunk_id": "chunk-b", "filename": "b.eml"},
            ],
            "retrieval_planes": {"direct": {"documents": 1}, "context": {"documents": 1}},
            "aggregations": {},
        }
    )
    service.read_document_chunks = AsyncMock()

    result = await service.search(
        "projet DDT",
        user_id="user-1",
        jwt_token="jwt",
        evidence_mode="focused",
    )

    assert {item["chunk_id"] for item in result["results"]} == {"chunk-a", "chunk-b"}
    service.read_document_chunks.assert_not_awaited()
    service.search_tool.assert_awaited_once_with(
        "projet DDT",
        embedding_model=None,
        progress_id=None,
    )


@pytest.mark.asyncio
async def test_direct_exhaustive_document_read_reports_verified_progress():
    from services.retrieval_progress_service import retrieval_progress_service

    progress_id = "directdocumentprogress"
    retrieval_progress_service.start(progress_id)
    service = SearchService(session_manager=MagicMock())
    service.read_document_chunks = AsyncMock(
        return_value={
            "results": [{"chunk_id": "chunk-1", "document_id": "document-1"}],
            "coverage": {
                "mode": "exhaustive",
                "document_id": "document-1",
                "covered_chunks": 1,
                "total_chunks": 1,
                "complete": True,
                "next_cursor": None,
            },
        }
    )

    result = await service.search(
        "document-1",
        user_id="user-1",
        jwt_token="jwt",
        evidence_mode="exhaustive",
        document_id="document-1",
        progress_id=progress_id,
    )

    assert result["coverage"]["complete"] is True
    snapshot = retrieval_progress_service.snapshot(progress_id)
    assert snapshot is not None
    assert snapshot["phase"] == "complete"
    assert snapshot["complete"] is True
    assert snapshot["failed"] is False
    assert snapshot["counters"] == {
        "documents_total": 1,
        "documents_read": 1,
        "chunks_read": 1,
        "chunks_total": 1,
    }


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


def test_normalized_score_fusion_calibrates_each_lane_before_combining_scores():
    lexical = [_hit("lexical", "a"), _hit("shared", "b")]
    vector = [_hit("shared", "b"), _hit("vector", "c")]
    lexical[0]["_score"] = 100.0
    lexical[1]["_score"] = 10.0
    vector[0]["_score"] = 0.99
    vector[1]["_score"] = 0.50

    fused = normalized_score_fusion([lexical, vector])

    # Raw BM25 magnitude never overwhelms the vector lane. The lexical-only
    # and vector-supported hits receive comparable normalized contributions.
    assert [hit["_id"] for hit in fused] == ["lexical", "shared", "vector"]
    assert fused[0]["_retrieval_normalized_score"] == pytest.approx(0.5)
    assert fused[1]["_retrieval_normalized_score"] == pytest.approx(0.5)
    assert fused[2]["_retrieval_normalized_score"] == pytest.approx(0.0)
    assert "_retrieval_normalized_score" not in lexical[0]


def test_normalized_score_fusion_does_not_invent_order_for_constant_lane_scores():
    b = _hit("b", "b")
    a = _hit("a", "a")
    b["_score"] = a["_score"] = 2.0

    fused = normalized_score_fusion([[b, a]])

    assert [hit["_id"] for hit in fused] == ["a", "b"]
    assert all(hit["_retrieval_normalized_score"] == 1.0 for hit in fused)


def test_consensus_fusion_keeps_both_policy_signals_without_new_candidates():
    lexical = [_hit("lexical", "a"), _hit("shared", "b")]
    vector = [_hit("shared", "b"), _hit("vector", "c")]
    lexical[0]["_score"] = 100.0
    lexical[1]["_score"] = 10.0
    vector[0]["_score"] = 0.99
    vector[1]["_score"] = 0.50

    fused = consensus_rank_fusion([lexical, vector], k=60)

    assert {hit["_id"] for hit in fused} == {"lexical", "shared", "vector"}
    assert [hit["_id"] for hit in fused] == ["lexical", "shared", "vector"]
    shared = fused[1]
    assert shared["_retrieval_fusion_ranks"] == {"rrf": 1, "normalized": 2}
    assert shared["_retrieval_rrf_score"] > 0
    assert shared["_retrieval_normalized_score"] == pytest.approx(0.5)
    assert "_retrieval_fusion_score" not in lexical[0]


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


@pytest.mark.asyncio
async def test_search_service_consensus_fuses_lanes_preserves_provenance_and_emits_debug(
    monkeypatch,
):
    """Standard retrieval converges rank and normalized score policies."""
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
    monkeypatch.setattr(search_service, "get_embedding_model", lambda: "test-model-a")
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
            "source_entity_id": "urn:mail:shared",
            "source_provenance": {
                "relations": [{"role": "reply_to", "target": {"id": "urn:mail:related"}}]
            },
        }
    )
    lexical_only = _hit("lexical", "document-a", "lexical text")
    lexical_only["_source"].update({"filename": "a.pdf", "chunk_index": 2})
    vector_same_document = _hit("vector-a", "document-a", "other text")
    related = _hit("related", "document-d", "Nous soutenons votre projet")
    related["_source"].update({"filename": "reply.eml", "source_entity_id": "urn:mail:related"})
    shared["_score"] = 0.9
    vector_same_document["_score"] = 0.8

    class OpenSearchClient:
        bodies: list[dict] = []
        scroll_bodies: list[dict] = []
        cleared_scrolls: list[dict] = []

        class indices:
            @staticmethod
            async def get_mapping(*, index):
                return {index: {"mappings": {"properties": {"chunk_id": {"type": "keyword"}}}}}

        async def scroll(self, *, body):
            self.scroll_bodies.append(body)
            return {
                "_scroll_id": "audit-scroll-2",
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [],
                },
            }

        async def clear_scroll(self, *, body):
            self.cleared_scrolls.append(body)
            return {"succeeded": True, "num_freed": 1}

        async def search(self, *, body, params, index=None):
            self.bodies.append(body)
            if body.get("size") == 0:
                if "audit_expansion_documents" in body.get("aggs", {}):
                    return {"aggregations": {"audit_expansion_documents": {"value": 3}}}
                return {
                    "aggregations": {
                        "embedding_models": {
                            "buckets": [
                                {"key": "test-model-a", "doc_count": 1_000},
                                {"key": "test-model-b", "doc_count": 1_000},
                            ]
                        },
                        "audit_scope_documents": {"value": 100},
                    }
                }
            if body.get("size") == 500:
                related["sort"] = [1.0, related["_id"]]
                return {
                    "_scroll_id": "audit-scroll-1",
                    "hits": {
                        "total": {"value": 1, "relation": "eq"},
                        "hits": [related],
                    },
                    "aggregations": {"data_sources": {"buckets": []}},
                }
            bool_query = body.get("query", {}).get("bool", {})
            must = bool_query.get("must", [])
            if must and "knn" in must[0]:
                vector_field = next(iter(must[0]["knn"]))
                hits = (
                    [shared, vector_same_document] if "test_model_a" in vector_field else [shared]
                )
            else:
                if params.get("scroll"):
                    hits = [lexical_only, shared]
                    for hit in hits:
                        hit["sort"] = [1.0, hit["_id"]]
                else:
                    hits = [lexical_only, shared]
            result = {
                "hits": {
                    "total": {
                        "value": 3 if params.get("scroll") else len(hits),
                        "relation": "eq",
                    },
                    "hits": hits,
                },
                "aggregations": {"data_sources": {"buckets": []}},
            }
            if params.get("scroll"):
                result["_scroll_id"] = "audit-scroll-1"
            return result

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
    opensearch_client = OpenSearchClient()
    session_manager.get_user_opensearch_client.return_value = opensearch_client
    service = SearchService(session_manager=session_manager)

    result = await service.search_tool("shared text")

    assert [item["chunk_id"] for item in result["results"]] == ["shared", "lexical", "related"]
    assert result["results"][0]["source_url"] == "https://example.test/b"
    assert result["results"][0]["document_id"] == "document-b"
    assert result["results"][0]["connector_file_id"] == "drive-file-b"
    assert result["results"][0]["filename"] == "b.pdf"
    assert result["results"][0]["page"] == 3
    assert result["results"][0]["chunk_index"] == 0
    assert result["results"][0]["chunking_strategy"] == "hybrid"
    assert result["retrieval_debug"]["lanes"] == {
        "lexical": 2,
        "vector:test-model-a": 2,
        "vector:test-model-b": 1,
    }
    assert result["retrieval_debug"]["configured_strategy"] == "rrf"
    assert result["retrieval_debug"]["effective_strategy"] == "rrf_normalized_consensus"
    assert result["retrieval_fusion"] == {
        "strategy": "rrf_normalized_consensus",
        "candidate_lanes": 3,
        "first_stage": [
            {"method": "rrf", "input": "rank"},
            {
                "method": "normalized_score",
                "normalization": "min_max_per_lane",
                "combination": "equal_weight_arithmetic_mean",
            },
        ],
        "final_stage": {"method": "rrf", "k": 60},
        "additional_opensearch_requests": 0,
        "llm_used": False,
    }
    lane_bodies = [body for body in OpenSearchClient.bodies if body.get("size") != 0]
    assert len(lane_bodies) == 5
    assert all(
        body["sort"]
        == [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ]
        for body in lane_bodies
    )
    assert result["provenance_retrieval"]["fixpoint_reached"] is True
    assert result["provenance_retrieval"]["canonical_field"] == (
        "source_provenance.relations.prov_predicate"
    )
    assert result["provenance_retrieval"]["predicates"] == sorted(PROVENANCE_TRAVERSAL_PREDICATES)
    assert result["provenance_retrieval"]["role_policy"] == {
        "purpose": "qualify_same_predicate_context",
        "excluded": ["contained_in"],
    }
    assert result["retrieval_planes"]["direct"]["documents"] == 2
    assert result["retrieval_planes"]["direct"]["ranking"] == "rrf_normalized_consensus"
    assert result["retrieval_planes"]["context"]["documents"] == 1
    assert result["results"][2]["retrieval_plane"] == "context"
    assert result["results"][2]["retrieval_relation_depth"] == 1
    assert result["results"][2]["retrieval_relevance"]["level"] == "contextual"
    assert result["noise_accounting"]["excluded_relation_documents"] == 0
