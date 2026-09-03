from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchmarks.metadata_planner import evaluate
from models.document_investigation import CalendarBasis
from models.metadata_agent_search import (
    MAX_AGENT_FILTERS,
    MetadataAgentFilter,
    MetadataAgentOperator,
    MetadataAgentQuery,
    MetadataPlanStatus,
    compile_agent_query,
)
from models.metadata_filter import MetadataFilterField
from services.metadata_query_planner import plan_metadata_query

ROOT = Path(__file__).resolve().parents[3]


def test_required_french_examples_are_deterministic_and_preserve_semantic_text():
    plan = plan_metadata_query("factures Orange PDF de mars 2024")

    assert plan.status == MetadataPlanStatus.VALID
    assert plan.free_text == "factures Orange"
    assert [item.canonical_payload() for item in plan.filters] == [
        {"field": "format_family", "operator": "EQUAL", "value": "pdf"},
        {
            "field": "production_month",
            "operator": "EQUAL",
            "value": "2024-03",
            "calendar_basis": "SOURCE_LOCAL",
        },
    ]

    invoice = plan_metadata_query("les factures de mars 2024")
    assert invoice.free_text == "factures"
    assert all(item.field != MetadataFilterField.SOURCE_DOCUMENT_TYPE for item in invoice.filters)


@pytest.mark.parametrize(
    ("query", "status", "diagnostic"),
    [
        ("les documents de mars", MetadataPlanStatus.AMBIGUOUS, "calendar_month_without_year"),
        (
            "les documents archivés en mars 2024",
            MetadataPlanStatus.UNSUPPORTED,
            "archive_or_ingestion_calendar",
        ),
        (
            "pièces jointes de ce mail",
            MetadataPlanStatus.UNSUPPORTED,
            "implicit_parent_source_identity",
        ),
    ],
)
def test_ambiguous_and_unsupported_constraints_fail_closed(query, status, diagnostic):
    plan = plan_metadata_query(query)

    assert plan.status == status
    assert plan.requires_metadata_search is False
    assert diagnostic in {*plan.ambiguities, *plan.unsupported_constraints}


def test_explicit_filters_are_preserved_and_override_inferred_field():
    explicit = MetadataAgentFilter(
        field=MetadataFilterField.PRODUCTION_YEAR,
        operator=MetadataAgentOperator.EQUAL,
        value="2023",
        calendar_basis=CalendarBasis.UTC,
    )

    plan = plan_metadata_query("documents produits en 2024", explicit_filters=(explicit,))

    assert plan.filters == (explicit,)


def test_agent_schema_compiles_not_equal_to_existing_fail_closed_negation():
    query = MetadataAgentQuery(
        free_text="documents",
        filters=(
            MetadataAgentFilter(
                field=MetadataFilterField.FORMAT_FAMILY,
                operator=MetadataAgentOperator.NOT_EQUAL,
                value="pdf",
            ),
        ),
    )

    compiled = compile_agent_query(query)

    assert compiled.clauses[0].operator.value == "EQUAL"
    assert compiled.clauses[0].negated is True


def test_agent_schema_rejects_raw_json_and_excessive_complexity():
    with pytest.raises(ValidationError):
        MetadataAgentFilter.model_validate(
            {"field": "format_family", "operator": "SCRIPT", "value": {"source": "x"}}
        )

    one = {
        "field": "format_family",
        "operator": "EQUAL",
        "value": "pdf",
    }
    with pytest.raises(ValidationError):
        MetadataAgentQuery.model_validate(
            {"free_text": "documents", "filters": [one] * (MAX_AGENT_FILTERS + 1)}
        )
    with pytest.raises(ValidationError):
        MetadataAgentQuery.model_validate(
            {
                "free_text": "documents",
                "filters": [
                    {
                        "field": "format_family",
                        "operator": "IN",
                        "value": [str(index) for index in range(17)],
                    }
                ],
            }
        )


def test_corpus_v1_is_exact_and_has_no_llm_cost():
    corpus = json.loads(
        (ROOT / "benchmarks" / "metadata-planner" / "corpus-v1.json").read_text(
            encoding="utf-8"
        )
    )

    result = evaluate(corpus)

    assert result["cases"] == 45
    assert result["exact_parse_accuracy"] == 1.0
    assert result["false_positive_filter_rate"] == 0.0
    assert result["false_negative_filter_rate"] == 0.0
    assert result["unsupported_accuracy"] == 1.0
    assert result["ambiguity_accuracy"] == 1.0
    assert result["free_text_preservation"] == 1.0
    assert result["llm_calls"] == 0
    assert result["additional_model_tokens"] == 0

