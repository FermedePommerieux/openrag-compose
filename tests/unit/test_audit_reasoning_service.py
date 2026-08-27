import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from services import audit_reasoning_service as audit_module
from services.ai_response_cache_service import AIResponseCacheService
from services.audit_reasoning_service import AuditReasoningService


class _MemoryStructuredCache:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def build_key(self, *, scope, model, schema_name, schema, prompt, namespace=None):
        import hashlib

        value = json.dumps(
            [scope, model, schema_name, schema, prompt, namespace],
            sort_keys=True,
        )
        return hashlib.sha256(value.encode()).hexdigest(), hashlib.sha256(
            scope.encode()
        ).hexdigest()

    async def get(self, cache_key):
        return self.items.get(cache_key)

    async def put(self, *, cache_key, response, usage, **_kwargs):
        self.items[cache_key] = {"response": response, "usage": usage or {}}


def _hit(document_id: str, text: str) -> dict:
    return {
        "_id": document_id,
        "_source": {
            "document_id": document_id,
            "filename": f"{document_id}.eml",
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_audit_query_expansion_keeps_only_grounded_unique_variants():
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "queries": [
                    "ASP DDT Nouan-le-Fuzelier",
                    "  ASP   DDT Nouan-le-Fuzelier  ",
                    "surface pastorale DDT",
                ],
                "entities": ["DDT 41", "DDT 41", "Nouan-le-Fuzelier"],
            }
        )
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    service = AuditReasoningService(client, "gpt-5.6-sol")

    expansion, metadata = await service.expand_query(
        "surface pastorale DDT",
        [
            _hit(
                "seed",
                "Anciennes surfaces pastorales ASP avec la DDT 41 à Nouan-le-Fuzelier",
            )
        ],
    )

    assert expansion.queries == ["ASP DDT Nouan-le-Fuzelier"]
    assert expansion.entities == ["DDT 41", "Nouan-le-Fuzelier"]
    assert metadata["available"] is True
    assert metadata["ungrounded_or_redundant_hints_rejected"] == 0
    request = client.responses.create.await_args.kwargs
    assert request["model"] == "gpt-5.6-sol"
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["timeout"] == 1_200.0


@pytest.mark.asyncio
async def test_audit_query_expansion_rejects_ungrounded_and_redundant_broad_hints():
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "queries": ["surface pastorale DDT 41", "unrelated wind turbines"],
                "entities": ["DDT", "DDT 41", "invented-case-999"],
            }
        )
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    service = AuditReasoningService(client, "gpt-5.6-luna")

    expansion, metadata = await service.expand_query(
        "surface pastorale DDT",
        [_hit("seed", "Dossier de surface pastorale suivi par la DDT 41")],
    )

    assert expansion.queries == ["surface pastorale DDT 41"]
    assert expansion.entities == ["DDT 41"]
    assert metadata["ungrounded_or_redundant_hints_rejected"] == 3


@pytest.mark.asyncio
async def test_identical_structured_audit_work_reuses_user_scoped_cache():
    response = SimpleNamespace(
        output_text=json.dumps({"queries": ["ASP DDT"], "entities": ["DDT 41"]}),
        usage=SimpleNamespace(input_tokens=500, output_tokens=25, total_tokens=525),
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    cache = _MemoryStructuredCache()
    service = AuditReasoningService(
        client,
        "gpt-5.6-luna",
        cache_scope="user-42",
        cache_service=cache,
    )
    seeds = [
        _hit(
            "seed",
            "ASP et sylvopastoralisme en Loir-et-Cher avec la DDT 41",
        )
    ]

    first, first_metadata = await service.expand_query("surface pastorale", seeds)
    second, second_metadata = await service.expand_query("surface pastorale", seeds)

    assert first == second
    client.responses.create.assert_awaited_once()
    assert first_metadata["application_cache"]["misses"] == 1
    assert second_metadata["application_cache"] == {
        "enabled": True,
        "hits": 1,
        "exact_hits": 1,
        "semantic_hits": 0,
        "related_hints": 0,
        "misses": 1,
        "scope": "user_evidence_contract_and_safe_query_equivalence",
    }


@pytest.mark.asyncio
async def test_changed_evidence_never_reuses_structured_audit_cache():
    response = SimpleNamespace(
        output_text=json.dumps({"queries": [], "entities": []}),
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    service = AuditReasoningService(
        client,
        "gpt-5.6-luna",
        cache_scope="user-42",
        cache_service=_MemoryStructuredCache(),
    )

    await service.expand_query("surface pastorale", [_hit("seed", "Version A")])
    await service.expand_query("surface pastorale", [_hit("seed", "Version B")])

    assert client.responses.create.await_count == 2
    assert service.cache_metadata()["hits"] == 0
    assert service.cache_metadata()["misses"] == 2


@pytest.mark.asyncio
async def test_minor_query_variation_reuses_same_evidence_contract_semantically():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    cache = AIResponseCacheService(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        ttl_days=0,
    )
    response = SimpleNamespace(
        output_text=json.dumps({"queries": ["ASP DDT"], "entities": ["DDT 41"]}),
        usage=SimpleNamespace(input_tokens=500, output_tokens=25, total_tokens=525),
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    embeddings = {"text-embedding-3-large": [0.3, -0.4, 0.1, 0.8, -0.2]}
    seeds = [_hit("seed", "Anciennes surfaces pastorales avec la DDT 41")]

    first_service = AuditReasoningService(
        client,
        "gpt-5.6-luna",
        cache_scope="user-42",
        cache_service=cache,
        query_embeddings=embeddings,
    )
    second_service = AuditReasoningService(
        client,
        "gpt-5.6-luna",
        cache_scope="user-42",
        cache_service=cache,
        query_embeddings=embeddings,
    )
    first, _metadata = await first_service.expand_query(
        "ancienne surface pastorale",
        seeds,
    )
    second, metadata = await second_service.expand_query(
        "anciennes surfaces pastorales",
        seeds,
    )

    assert first == second
    client.responses.create.assert_awaited_once()
    assert metadata["application_cache"]["semantic_hits"] == 1
    assert metadata["application_cache"]["exact_hits"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_logic_change_never_reuses_semantic_cache_even_with_same_embedding():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    cache = AIResponseCacheService(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        ttl_days=0,
    )
    response = SimpleNamespace(
        output_text=json.dumps({"queries": [], "entities": []}),
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    embeddings = {"text-embedding-3-large": [0.3, -0.4, 0.1, 0.8, -0.2]}
    seeds = [_hit("seed", "Échanges administratifs datés de 2020")]

    for query in ("échanges avant 2020", "échanges après 2020"):
        service = AuditReasoningService(
            client,
            "gpt-5.6-luna",
            cache_scope="user-42",
            cache_service=cache,
            query_embeddings=embeddings,
        )
        await service.expand_query(query, seeds)

    assert client.responses.create.await_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_related_query_reuses_prior_expansion_only_as_additive_discovery_memory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    cache = AIResponseCacheService(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        ttl_days=0,
    )
    responses = [
        SimpleNamespace(output_text=json.dumps({"queries": ["ASP DDT"], "entities": ["DDT 41"]})),
        SimpleNamespace(
            output_text=json.dumps(
                {
                    "queries": ["sylvopastoralisme DDT"],
                    "entities": ["Loir-et-Cher"],
                }
            )
        ),
    ]
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=responses)))
    embeddings = {"text-embedding-3-large": [0.3, -0.4, 0.1, 0.8, -0.2]}
    seeds = [
        _hit(
            "seed",
            "ASP et sylvopastoralisme en Loir-et-Cher avec la DDT 41",
        )
    ]

    first_service = AuditReasoningService(
        client,
        "gpt-5.6-luna",
        cache_scope="user-42",
        cache_service=cache,
        query_embeddings=embeddings,
    )
    await first_service.expand_query("surface pastorale DDT", seeds)
    second_service = AuditReasoningService(
        client,
        "gpt-5.6-luna",
        cache_scope="user-42",
        cache_service=cache,
        query_embeddings=embeddings,
    )
    expansion, metadata = await second_service.expand_query("projet pastoral DDT", seeds)

    assert client.responses.create.await_count == 2
    assert expansion.queries == ["sylvopastoralisme DDT", "ASP DDT"]
    assert expansion.entities == ["Loir-et-Cher", "DDT 41"]
    assert metadata["related_research_memory"] == {
        "used": True,
        "queries_added": 1,
        "entities_added": 1,
        "role": "additive_discovery_hint_only",
    }
    assert metadata["application_cache"]["semantic_hits"] == 0
    assert metadata["application_cache"]["related_hints"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_contextual_review_labels_noise_but_never_excludes_a_document():
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "decisions": [
                    {
                        "document_id": "relevant",
                        "decision": "relevant",
                        "reason": "The relation path resolves 'your project' to the anchor.",
                        "supporting_document_ids": ["anchor"],
                    },
                    {
                        "document_id": "noise",
                        "decision": "irrelevant",
                        "reason": "The message concerns an unrelated veterinary notice.",
                        "supporting_document_ids": [],
                    },
                ]
            }
        )
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    service = AuditReasoningService(client, "gpt-5.6-sol")
    relevant = _hit("relevant", "Nous soutenons votre projet")
    relevant["_source"]["retrieval_relation_paths"] = [
        {
            "from_document_id": "anchor",
            "to_document_id": "relevant",
            "relation_role": "reply_to",
        }
    ]
    missing = _hit("missing", "Insufficient excerpt")

    retained, metadata = await service.review_candidates(
        "surface pastorale DDT",
        [
            relevant,
            _hit("noise", "Vaccination bovine"),
            missing,
        ],
    )

    assert [hit["_source"]["document_id"] for hit in retained] == [
        "relevant",
        "noise",
        "missing",
    ]
    assert missing["_source"]["retrieval_relevance_decision"] == "uncertain"
    assert metadata == {
        "available": True,
        "model": "gpt-5.6-sol",
        "transport_batches": 1,
        "failed_batches": 0,
        "invalid_decisions": 0,
        "missing_decisions": 1,
        "reviewed_documents": 3,
        "retained_documents": 3,
        "excluded_documents": 0,
        "advisory_irrelevant_documents": 1,
        "selection_policy": "all_discovered_candidates_read",
        "pre_read_exclusion_applied": False,
        "relevant": 1,
        "uncertain": 1,
        "irrelevant": 1,
    }


@pytest.mark.asyncio
async def test_contextual_review_failure_retains_every_candidate():
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("offline")))
    )
    service = AuditReasoningService(client, "gpt-5.6-sol")
    hits = [_hit("one", "one"), _hit("two", "two")]

    retained, metadata = await service.review_candidates("query", hits)

    assert retained == hits
    assert metadata["available"] is False
    assert metadata["failed_batches"] == 1
    assert metadata["missing_decisions"] == 2
    assert metadata["uncertain"] == 2


@pytest.mark.asyncio
async def test_contextual_review_rejects_an_invented_supporting_identity():
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "decisions": [
                    {
                        "document_id": "candidate",
                        "decision": "irrelevant",
                        "reason": "Claims another project.",
                        "supporting_document_ids": ["invented-document"],
                    }
                ]
            }
        )
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    service = AuditReasoningService(client, "gpt-5.6-sol")
    candidate = _hit("candidate", "Nous soutenons votre projet")

    retained, metadata = await service.review_candidates("surface pastorale", [candidate])

    assert retained == [candidate]
    assert candidate["_source"]["retrieval_relevance_decision"] == "uncertain"
    assert metadata["invalid_decisions"] == 1
    assert metadata["missing_decisions"] == 1


def _orchestrator_response(**request) -> SimpleNamespace:
    """Return source-faithful structured data for every orchestration role."""
    name = request["text"]["format"]["name"]
    prompt = request["input"][1]["content"]
    if name in {"audit_evidence_extractor_memo", "audit_skeptical_verifier_memo"}:
        evidence = json.loads(prompt.split("Evidence batch JSON:\n", 1)[1])
        chunk_ids = list(dict.fromkeys(item["chunk_id"] for item in evidence))
        document_ids = list(dict.fromkeys(item["document_id"] for item in evidence))
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "assessment": "relevant",
                    "summary": "The batch contains a source-grounded administrative exchange.",
                    "findings": [
                        {
                            "category": "administrative_exchange",
                            "statement": f"Verified exchange in {chunk_ids[0]}.",
                            "chunk_ids": [chunk_ids[0]],
                            "document_ids": [document_ids[0]],
                        }
                    ],
                    "unresolved_questions": [],
                    "covered_chunk_ids": chunk_ids,
                }
            )
        )
    if name == "audit_claim_verdicts":
        findings_json = prompt.split("Findings JSON:\n", 1)[1].split("\nEvidence JSON:\n", 1)[0]
        findings = json.loads(findings_json)
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "verdicts": [
                        {
                            "finding_id": finding["finding_id"],
                            "verdict": "supported",
                            "reason": "The supplied source evidence directly entails it.",
                            "supporting_chunk_ids": [finding["chunk_ids"][0]],
                        }
                        for finding in findings
                    ]
                }
            )
        )
    if name == "audit_coordinator_report":
        inputs = json.loads(prompt.split("Coordinator inputs JSON:\n", 1)[1])
        findings = []
        for item in inputs:
            for finding in item["findings"]:
                source_ids = finding.get("source_finding_ids") or [finding["finding_id"]]
                findings.append(
                    {
                        "category": finding["category"],
                        "statement": finding["statement"],
                        "chunk_ids": finding["chunk_ids"],
                        "document_ids": finding["document_ids"],
                        "source_finding_ids": source_ids,
                    }
                )
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "executive_summary": "All verified findings were retained.",
                    "findings": findings,
                    "unresolved_questions": [],
                    "covered_input_ids": [
                        item.get("memo_id") or item["report_id"] for item in inputs
                    ],
                }
            )
        )
    raise AssertionError(f"Unexpected structured output name: {name}")


@pytest.mark.asyncio
async def test_hierarchical_orchestrator_covers_every_chunk_and_finding(monkeypatch):
    monkeypatch.setattr(audit_module, "AUDIT_SYNTHESIS_BATCH_CHUNKS", 1)
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=_orchestrator_response))
    )
    service = AuditReasoningService(client, "gpt-5.6-sol")
    chunks = [
        {
            "chunk_id": "chunk-a",
            "document_id": "document-a",
            "filename": "a.eml",
            "text": "La DDT confirme la réception du dossier.",
        },
        {
            "chunk_id": "chunk-b",
            "document_id": "document-b",
            "filename": "b.eml",
            "text": "Réponse administrative relative au projet.",
        },
    ]

    synthesis, coverage = await service.synthesize_evidence("projet DDT", chunks)

    assert synthesis["complete"] is True
    assert synthesis["verified"] is True
    assert {chunk_id for finding in synthesis["findings"] for chunk_id in finding["chunk_ids"]} == {
        "chunk-a",
        "chunk-b",
    }
    assert synthesis["withheld_findings"] == []
    assert coverage["chunks_total"] == coverage["chunks_covered"] == 2
    assert coverage["map_batches"] == coverage["map_batches_complete"] == 2
    assert coverage["leaf_workers_expected"] == coverage["leaf_workers_succeeded"] == 4
    assert coverage["final_claim_validators_expected"] == 2
    assert coverage["final_claim_validators_succeeded"] == 2
    assert coverage["answer_claims_total"] == 2
    assert coverage["answer_source_chunks_verified"] == 2
    assert coverage["inverse_finding_coverage_complete"] is True
    assert coverage["reduce_levels"] == 1
    assert synthesis["answer_contract"] == {
        "claim_source": "findings",
        "verification_direction": "answer_claims_against_cited_source_chunks",
        "all_verified_findings_must_be_represented": True,
        "unsupported_claims_forbidden": True,
    }

    final_verifier_calls = [
        call.kwargs
        for call in client.responses.create.await_args_list
        if call.kwargs["text"]["format"]["name"] == "audit_claim_verdicts"
        and "audit-final-finding" in call.kwargs["input"][1]["content"]
    ]
    assert len(final_verifier_calls) == 2
    assert all(
        "Evidence batch JSON" not in call["input"][1]["content"] for call in final_verifier_calls
    )
    assert all(
        "La DDT confirme" in call["input"][1]["content"]
        or "Réponse administrative" in call["input"][1]["content"]
        for call in final_verifier_calls
    )


@pytest.mark.asyncio
async def test_hierarchical_orchestrator_fails_closed_when_one_leaf_reader_fails():
    def partial_response(**request):
        if request["text"]["format"]["name"] == "audit_skeptical_verifier_memo":
            raise RuntimeError("skeptical reader unavailable")
        return _orchestrator_response(**request)

    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=partial_response))
    )
    service = AuditReasoningService(client, "gpt-5.6-sol")

    synthesis, coverage = await service.synthesize_evidence(
        "projet DDT",
        [
            {
                "chunk_id": "chunk-a",
                "document_id": "document-a",
                "text": "La DDT confirme la réception du dossier.",
            }
        ],
    )

    assert synthesis["complete"] is False
    assert synthesis["verified"] is False
    assert coverage["leaf_dual_review_complete"] is False
    assert coverage["leaf_workers_expected"] == 2
    assert coverage["leaf_workers_succeeded"] == 1


@pytest.mark.asyncio
async def test_coordinator_rejects_citation_from_an_unrepresented_finding():
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "executive_summary": "Invalid cross-citation.",
                "findings": [
                    {
                        "category": "fact",
                        "statement": "Statement A with citation B.",
                        "chunk_ids": ["chunk-b"],
                        "document_ids": ["document-b"],
                        "source_finding_ids": ["finding-a"],
                    },
                    {
                        "category": "fact",
                        "statement": "Statement B.",
                        "chunk_ids": ["chunk-b"],
                        "document_ids": ["document-b"],
                        "source_finding_ids": ["finding-b"],
                    },
                ],
                "unresolved_questions": [],
                "covered_input_ids": ["memo-a", "memo-b"],
            }
        )
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    service = AuditReasoningService(client, "gpt-5.6-sol")
    group = [
        {
            "memo_id": "memo-a",
            "findings": [
                {
                    "finding_id": "finding-a",
                    "category": "fact",
                    "statement": "Statement A.",
                    "chunk_ids": ["chunk-a"],
                    "document_ids": ["document-a"],
                }
            ],
        },
        {
            "memo_id": "memo-b",
            "findings": [
                {
                    "finding_id": "finding-b",
                    "category": "fact",
                    "statement": "Statement B.",
                    "chunk_ids": ["chunk-b"],
                    "document_ids": ["document-b"],
                }
            ],
        },
    ]

    with pytest.raises(ValueError, match="invented a chunk citation"):
        await service._coordinate_group(query="projet DDT", group=group, report_id="report")
