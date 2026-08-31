import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from auth_context import get_auth_context, get_search_filters
from services.retrieval_service import (
    DiscoveryQuery,
    build_discovery_plan,
    discovery_query_prompt,
    multi_query_reciprocal_rank_fusion,
    normalize_discovery_query,
)
from services.search_service import SearchService


def _query(query_id: str, text: str, kind: str = "conceptual_variant") -> DiscoveryQuery:
    return DiscoveryQuery(
        query_id=query_id,
        query_text=text,
        query_type="original" if query_id == "q0" else kind,
        parent_query="all records about Project Z",
        generation_method="user" if query_id == "q0" else "test",
    )


def _result(chunk_id: str, document_id: str, query: DiscoveryQuery, rank: int) -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "filename": f"{document_id}.txt",
        "text": chunk_id,
        "score": 1 / (60 + rank),
        "matched_queries": [query.query_id],
        "matched_lanes": ["lexical", "dense"],
        "best_rank_per_query": {query.query_id: rank},
        "query_contributions": [
            {
                **query.as_dict(),
                "lexical_rank": rank,
                "dense_rank": rank + 1,
                "rrf_rank": rank,
                "matched_lanes": ["lexical", "dense"],
            }
        ],
    }


def test_normalization_deduplicates_case_accents_whitespace_and_punctuation():
    assert normalize_discovery_query("  Décision—B! ") == "decision b"
    assert normalize_discovery_query("décision B") == "decision b"


def test_original_is_always_q0_and_generated_duplicates_are_removed():
    plan = build_discovery_plan(
        "Decision B",
        {
            "queries": [
                {"text": " décision-b ", "kind": "conceptual_variant"},
                {"text": "appeal and review of Decision B", "kind": "administrative_legal"},
                {"text": "", "kind": "entity_focus"},
            ]
        },
        max_queries=4,
    )

    assert [item.query_id for item in plan] == ["q0", "q1"]
    assert plan[0].query_text == "Decision B"
    assert plan[0].generation_method == "user"
    assert plan[1].parent_query == "Decision B"


def test_simple_query_may_keep_only_the_original():
    plan = build_discovery_plan("invoice INV-1042", {"queries": []}, max_queries=4)
    assert [item.query_text for item in plan] == ["invoice INV-1042"]


@pytest.mark.parametrize(
    ("query", "generated", "kind"),
    [
        ("agreement renewal", "agreement extension amendment", "historical_wording"),
        (
            "records about Project Z",
            "Project Z correspondence and decisions",
            "documentary_subject",
        ),
        (
            "letters concerning parcel A",
            "parcel A notices and authorisations",
            "administrative_legal",
        ),
        (
            "messages in the incident thread",
            "replies and attachments for the incident",
            "relationship_event",
        ),
        ("standalone technical note N", "technical note N", "entity_focus"),
    ],
)
def test_generic_domains_require_no_case_specific_rules(query, generated, kind):
    plan = build_discovery_plan(
        query,
        {"queries": [{"text": generated, "kind": kind}]},
        max_queries=2,
    )
    assert [item.query_type for item in plan] == ["original", kind]


def test_max_queries_is_a_hard_cap_after_validation():
    plan = build_discovery_plan(
        "contract X",
        {
            "queries": [
                {"text": f"variant {index}", "kind": "conceptual_variant"} for index in range(20)
            ]
        },
        max_queries=3,
    )
    assert len(plan) == 3


def test_query_planner_prompt_has_no_benchmark_or_review_input():
    prompt = discovery_query_prompt("all records about Project Z", max_queries=4).casefold()
    forbidden_inputs = (
        "final-baseline.json",
        "ground truth labels",
        "control search probes",
        "miss list",
        "human review files",
    )
    assert all(value not in prompt for value in forbidden_inputs)
    assert "at most 3 additional" in prompt


def test_generic_engine_source_has_no_case_specific_terms():
    root = Path(__file__).resolve().parents[2]
    source = "\n".join(
        (root / path).read_text(encoding="utf-8").casefold()
        for path in ("src/services/retrieval_service.py", "src/services/search_service.py")
    )
    forbidden = (
        "surface pastorale",
        "pommerieux",
        "ddt",
        "préfecture",
        "défrichement",
        "psg",
        "pastoralisme",
    )
    assert all(re.search(rf"\b{re.escape(value)}\b", source) is None for value in forbidden)


def test_hierarchical_rrf_is_stable_deduplicated_and_traceable():
    q0 = _query("q0", "all records about Project Z")
    q1 = _query("q1", "Project Z correspondence")
    inputs = [
        (q0, [_result("shared", "doc-a", q0, 1), _result("q0-only", "doc-b", q0, 2)]),
        (q1, [_result("shared", "doc-a", q1, 1), _result("q1-only", "doc-c", q1, 2)]),
    ]

    first = multi_query_reciprocal_rank_fusion(inputs, k=60)
    second = multi_query_reciprocal_rank_fusion(inputs, k=60)

    assert [item["chunk_id"] for item in first] == ["shared", "q0-only", "q1-only"]
    assert first == second
    assert first[0]["matched_queries"] == ["q0", "q1"]
    assert first[0]["matched_lanes"] == ["dense", "lexical"]
    assert first[0]["best_rank_per_query"] == {"q0": 1, "q1": 1}
    assert first[0]["fusion_score"] > first[1]["fusion_score"]


@pytest.mark.asyncio
async def test_multi_query_reuses_dls_filters_and_respects_global_budget(monkeypatch):
    from services import search_service

    knowledge = SimpleNamespace(
        retrieval_strategy="rrf",
        retrieval_mode="hybrid",
        retrieval_rrf_k=60,
        retrieval_max_chunks_per_document=3,
        retrieval_adaptive_max_chunks_per_document=20,
    )
    monkeypatch.setattr(
        search_service,
        "get_openrag_config",
        lambda: SimpleNamespace(
            knowledge=knowledge,
            providers=SimpleNamespace(ollama=SimpleNamespace(endpoint="")),
        ),
    )
    service = SearchService.__new__(SearchService)
    service.models_service = None
    q0 = _query("q0", "all records about Project Z")
    q1 = _query("q1", "Project Z correspondence")
    q2 = _query("q2", "Project Z decisions")
    service._generate_discovery_plan = AsyncMock(return_value=([q0, q1, q2], None, 0.01))
    observations: list[tuple[str, tuple, dict]] = []

    async def fake_search(query, **kwargs):
        item = kwargs["_discovery_query"]
        observations.append((query, get_auth_context(), get_search_filters()))
        shared = _result("shared", "doc-shared", item, 1)
        unique = _result(f"{item.query_id}-unique", f"doc-{item.query_id}", item, 2)
        return {
            "results": [shared, unique],
            "aggregations": {},
            "total": 2,
            "_retrieval_timing": {
                "lexical_seconds": 0.1,
                "dense_seconds": 0.2,
                "embedding_seconds": 0.05,
                "fusion_seconds": 0.01,
                "total_seconds": 0.25,
            },
        }

    service.search_tool = AsyncMock(side_effect=fake_search)
    result = await service.search(
        q0.query_text,
        user_id="user-1",
        jwt_token="jwt-1",
        filters={"owners": ["owner-1"]},
        limit=3,
        multi_query_discovery=True,
        multi_query_max_queries=3,
        multi_query_concurrency=2,
    )

    assert len(observations) == 3
    assert all(auth == ("user-1", "jwt-1") for _, auth, _ in observations)
    assert all(filters == {"owners": ["owner-1"]} for _, _, filters in observations)
    assert len(result["results"]) == 3
    assert result["discovery"]["final_seed_chunk_budget"] == 3
    assert result["discovery"]["duplicate_seed_ratio"] == pytest.approx(2 / 6)
    assert result["discovery"]["query_count"] == 3


@pytest.mark.asyncio
async def test_disabled_mode_preserves_single_query_call(monkeypatch):
    from services import search_service

    monkeypatch.setattr(search_service, "is_no_auth_mode", lambda: False, raising=False)
    service = SearchService.__new__(SearchService)
    service.search_tool = AsyncMock(return_value={"results": [{"chunk_id": "original"}]})

    result = await service.search(
        "literal query",
        user_id="user-1",
        jwt_token="jwt-1",
        limit=7,
    )

    assert result == {"results": [{"chunk_id": "original"}]}
    service.search_tool.assert_awaited_once_with(
        "literal query",
        embedding_model=None,
        group_by_document=False,
        page=1,
        page_size=100,
    )


@pytest.mark.asyncio
async def test_multi_query_count_one_preserves_q0_order_and_scores(monkeypatch):
    from services import search_service

    knowledge = SimpleNamespace(retrieval_strategy="rrf", retrieval_mode="hybrid")
    monkeypatch.setattr(
        search_service,
        "get_openrag_config",
        lambda: SimpleNamespace(knowledge=knowledge),
    )
    service = SearchService.__new__(SearchService)
    service.models_service = None
    q0 = _query("q0", "all records about Project Z")
    service._generate_discovery_plan = AsyncMock(return_value=([q0], None, 0.0))
    expected = [_result("b", "doc-b", q0, 1), _result("a", "doc-a", q0, 2)]
    service.search_tool = AsyncMock(
        return_value={
            "results": expected,
            "aggregations": {},
            "_retrieval_timing": {},
        }
    )

    from auth_context import set_search_limit

    set_search_limit(96)
    result = await service._search_multi_query(
        q0.query_text,
        embedding_model=None,
        max_queries=1,
        concurrency=1,
    )

    assert result["results"] == expected
    assert result["discovery"]["query_count"] == 1


@pytest.mark.asyncio
async def test_planner_failure_keeps_q0_results_but_marks_discovery_incomplete(monkeypatch):
    from services import search_service

    knowledge = SimpleNamespace(retrieval_strategy="rrf", retrieval_mode="hybrid")
    monkeypatch.setattr(
        search_service,
        "get_openrag_config",
        lambda: SimpleNamespace(knowledge=knowledge),
    )
    service = SearchService.__new__(SearchService)
    service.models_service = None
    q0 = _query("q0", "all records about Project Z")
    service._generate_discovery_plan = AsyncMock(
        return_value=([q0], "planner rejected request", 0.01)
    )
    expected = [_result("q0-result", "doc-q0", q0, 1)]
    service.search_tool = AsyncMock(
        return_value={
            "results": expected,
            "aggregations": {},
            "_retrieval_timing": {},
            "requested_retrieval_profile": {
                "version": 1,
                "strategy": "rrf",
                "mode": "hybrid",
                "lanes": {
                    "lexical": "required",
                    "dense": "required",
                    "fusion": "required",
                    "multi_query": "disabled",
                },
            },
            "effective_retrieval_profile": {
                "version": 1,
                "strategy": "rrf",
                "mode": "hybrid",
                "lanes": {
                    "lexical": {"status": "succeeded", "candidates": 1},
                    "dense": {"status": "succeeded", "candidates": 1},
                    "fusion": {"status": "succeeded", "candidates": 1},
                    "multi_query": {"status": "not_requested", "candidates": 0},
                },
            },
            "retrieval_execution_complete": True,
            "retrieval_failure_codes": [],
        }
    )

    result = await service._search_multi_query(
        q0.query_text,
        embedding_model=None,
        max_queries=4,
        concurrency=1,
    )

    assert result["results"] == expected
    assert result["discovery"]["multi_query_requested"] is True
    assert result["discovery"]["multi_query_executed"] is False
    assert result["discovery"]["multi_query_query_count"] == 1
    assert result["discovery"]["multi_query_status"] == "planner_failed"
    assert result["retrieval_execution_complete"] is False
    assert "multi_query_planner_failed" in result["retrieval_failure_codes"]
    assert any(
        warning["code"] == "query_decomposition_unavailable" for warning in result["warnings"]
    )
