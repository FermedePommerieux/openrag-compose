"""Fail-closed tests for ``openrag.metadata-filter v1``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from models.document_investigation import CalendarBasis
from models.document_metadata import (
    DocumentMetadataProfile,
    MetadataNormalizationStatus,
    MetadataObservation,
    MetadataSectionName,
    MetadataSourceType,
    MetadataTrustClass,
)
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterClause,
    MetadataFilterConjunction,
    MetadataFilterContextValue,
    MetadataFilterDocumentContext,
    MetadataFilterField,
    MetadataFilterOperator,
    MetadataProfileAvailability,
    MetadataTruthValue,
)
from models.source_provenance import SourceEntity, SourceProvenance
from services.document_investigation import inspect_document_metadata
from services.metadata_filter import evaluate_metadata_filter

_EXTRACTED_AT = datetime(2026, 9, 3, tzinfo=UTC)


def _observation(
    field: str,
    value: str | None,
    *,
    source: str = "pdf_info_dictionary",
) -> MetadataObservation:
    temporal = field.startswith("embedded_") and field.endswith("_at")
    if temporal:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        explicit = parsed is not None and parsed.tzinfo is not None
        return MetadataObservation(
            section=MetadataSectionName.EMBEDDED,
            field=field,
            value=value,
            raw_value=value,
            source=source,
            source_type=MetadataSourceType.FORMAT_NATIVE,
            trust_class=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
            extracted_at=_EXTRACTED_AT,
            normalization_status=(
                MetadataNormalizationStatus.TIMEZONE_EXPLICIT
                if explicit
                else MetadataNormalizationStatus.TIMEZONE_UNKNOWN
            ),
            timezone="Z" if explicit else "UNKNOWN",
        )
    section = (
        MetadataSectionName.IDENTITY
        if field in {"mime_type", "extension", "original_filename", "sha256"}
        else MetadataSectionName.EMBEDDED
    )
    return MetadataObservation(
        section=section,
        field=field,
        value=value,
        source=source,
        source_type=MetadataSourceType.FORMAT_NATIVE,
        trust_class=MetadataTrustClass.EMBEDDED_DOCUMENT_METADATA,
        extracted_at=_EXTRACTED_AT,
        normalization_status=MetadataNormalizationStatus.NORMALIZED,
    )


def _inspection(
    *observations: MetadataObservation,
    source_system: str | None = None,
):
    sections: dict[MetadataSectionName, list[MetadataObservation]] = {
        section: [] for section in MetadataSectionName
    }
    for observation in observations:
        sections[observation.section].append(observation)
    profile = DocumentMetadataProfile(
        entity_id="urn:test:document",
        identity=sections[MetadataSectionName.IDENTITY],
        embedded=sections[MetadataSectionName.EMBEDDED],
        filesystem=sections[MetadataSectionName.FILESYSTEM],
        archive=sections[MetadataSectionName.ARCHIVE],
        ingestion=sections[MetadataSectionName.INGESTION],
    )
    provenance = (
        SourceProvenance(
            entity=SourceEntity(
                id=profile.entity_id,
                type="file",
                source_system=source_system,
            )
        )
        if source_system
        else None
    )
    return inspect_document_metadata(profile, source_provenance=provenance)


def _temporal_clause(
    field: MetadataFilterField,
    value: str,
    *,
    basis: CalendarBasis = CalendarBasis.SOURCE_LOCAL,
    source_policy: MetadataDateSourcePolicy | None = None,
    explicit_source: str | None = None,
    negated: bool = False,
) -> MetadataFilterClause:
    role_policy = (
        MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION
        if field.value.startswith("production_")
        else MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION
    )
    return MetadataFilterClause(
        field=field,
        operator=MetadataFilterOperator.EQUAL,
        values=(value,),
        calendar_basis=basis,
        source_policy=source_policy or role_policy,
        explicit_source=explicit_source,
        negated=negated,
    )


def _evaluate(clause: MetadataFilterClause, inspection=None, **kwargs):
    connector = kwargs.pop("connector", None)
    context = kwargs.pop("context", None)
    if connector:
        context = MetadataFilterDocumentContext(
            document_id="urn:test:document",
            values=(
                MetadataFilterContextValue(
                    field=MetadataFilterField.CONNECTOR,
                    value=connector,
                    source="indexed_document.connector_type",
                ),
            ),
            complete_fields=frozenset({MetadataFilterField.CONNECTOR}),
        )
    return evaluate_metadata_filter(
        MetadataFilter(clauses=(clause,)),
        document_id="urn:test:document",
        inspection=inspection,
        profile_availability=kwargs.pop(
            "profile_availability", MetadataProfileAvailability.AVAILABLE
        ),
        context=context,
        **kwargs,
    )


def test_missing_value_and_its_negation_are_unknown_and_fail_closed():
    inspection = _inspection()
    clause = _temporal_clause(MetadataFilterField.PRODUCTION_MONTH, "2024-03")
    positive = _evaluate(clause, inspection)
    negative = _evaluate(clause.model_copy(update={"negated": True}), inspection)

    assert positive.result == MetadataTruthValue.UNKNOWN
    assert negative.result == MetadataTruthValue.UNKNOWN
    assert positive.matches is False
    assert negative.matches is False


@pytest.mark.parametrize(
    "availability",
    [
        MetadataProfileAvailability.EXTRACTION_IMPOSSIBLE,
        MetadataProfileAvailability.ARCHIVE_SOURCE_UNAVAILABLE,
    ],
)
def test_unavailable_profiles_are_unknown_even_for_not_exists(availability):
    clause = MetadataFilterClause(
        field=MetadataFilterField.CREATOR_OBSERVATION,
        operator=MetadataFilterOperator.NOT_EXISTS,
    )

    assert _evaluate(clause, profile_availability=availability).result == MetadataTruthValue.UNKNOWN


def test_unavailable_profile_can_use_independently_complete_structural_context():
    clause = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.EQUAL,
        values=("application/pdf",),
    )
    context = MetadataFilterDocumentContext(
        document_id="urn:test:document",
        values=(
            MetadataFilterContextValue(
                field=MetadataFilterField.MIME,
                value="application/pdf",
                source="indexed_document.mimetype",
            ),
        ),
        complete_fields=frozenset({MetadataFilterField.MIME}),
    )

    evaluation = _evaluate(
        clause,
        profile_availability=MetadataProfileAvailability.EXTRACTION_IMPOSSIBLE,
        context=context,
    )

    assert evaluation.result == MetadataTruthValue.TRUE


def test_exists_and_not_exists_distinguish_known_absence_from_unavailability():
    inspection = _inspection()
    exists = MetadataFilterClause(
        field=MetadataFilterField.CREATOR_OBSERVATION,
        operator=MetadataFilterOperator.EXISTS,
    )
    not_exists = exists.model_copy(update={"operator": MetadataFilterOperator.NOT_EXISTS})

    assert _evaluate(exists, inspection).result == MetadataTruthValue.FALSE
    assert _evaluate(not_exists, inspection).result == MetadataTruthValue.TRUE


def test_timezone_unknown_matches_source_local_month_but_not_utc_month():
    inspection = _inspection(_observation("embedded_created_at", "2024-03-31T23:30:00"))
    local = _evaluate(_temporal_clause(MetadataFilterField.PRODUCTION_MONTH, "2024-03"), inspection)
    utc = _evaluate(
        _temporal_clause(
            MetadataFilterField.PRODUCTION_MONTH,
            "2024-03",
            basis=CalendarBasis.UTC,
        ),
        inspection,
    )

    assert local.result == MetadataTruthValue.TRUE
    assert utc.result == MetadataTruthValue.UNKNOWN


def test_explicit_offset_can_cross_source_local_month_and_year_in_utc():
    inspection = _inspection(_observation("embedded_created_at", "2024-01-01T00:30:00+02:00"))
    local = _evaluate(_temporal_clause(MetadataFilterField.PRODUCTION_YEAR, "2024"), inspection)
    utc = _evaluate(
        _temporal_clause(
            MetadataFilterField.PRODUCTION_YEAR,
            "2023",
            basis=CalendarBasis.UTC,
        ),
        inspection,
    )

    assert local.result == MetadataTruthValue.TRUE
    assert utc.result == MetadataTruthValue.TRUE


def test_multi_observation_match_preserves_conflicting_evidence():
    inspection = _inspection(
        _observation("embedded_created_at", "2024-03-10T12:00:00Z", source="pdf_info_dictionary"),
        _observation("embedded_created_at", "2024-04-10T12:00:00Z", source="pdf_xmp"),
    )
    evaluation = _evaluate(
        _temporal_clause(MetadataFilterField.PRODUCTION_MONTH, "2024-03"), inspection
    )

    assert evaluation.result == MetadataTruthValue.TRUE
    assert {item.source for item in evaluation.matched_observations} == {"pdf_info_dictionary"}
    assert {item.source for item in evaluation.conflicting_observations} == {"pdf_xmp"}


def test_explicit_source_does_not_choose_a_preferred_global_timestamp():
    inspection = _inspection(
        _observation("embedded_created_at", "2024-03-10T12:00:00Z", source="pdf_info_dictionary"),
        _observation("embedded_created_at", "2024-04-10T12:00:00Z", source="pdf_xmp"),
    )
    clause = _temporal_clause(
        MetadataFilterField.PRODUCTION_MONTH,
        "2024-03",
        source_policy=MetadataDateSourcePolicy.EXPLICIT_SOURCE,
        explicit_source="pdf_xmp",
    )

    assert _evaluate(clause, inspection).result == MetadataTruthValue.FALSE


@pytest.mark.parametrize(
    "clause,observations,source_system,connector",
    [
        (
            MetadataFilterClause(
                field=MetadataFilterField.MIME,
                operator=MetadataFilterOperator.EQUAL,
                values=("APPLICATION/PDF; charset=binary",),
            ),
            (_observation("mime_type", "application/pdf"),),
            None,
            None,
        ),
        (
            MetadataFilterClause(
                field=MetadataFilterField.CREATOR_OBSERVATION,
                operator=MetadataFilterOperator.EQUAL,
                values=("  ALICE  SMITH ",),
            ),
            (_observation("creator", "Alice Smith"),),
            None,
            None,
        ),
        (
            MetadataFilterClause(
                field=MetadataFilterField.SOURCE_SYSTEM,
                operator=MetadataFilterOperator.EQUAL,
                values=("OpenArchiver",),
            ),
            (),
            "openarchiver",
            None,
        ),
        (
            MetadataFilterClause(
                field=MetadataFilterField.CONNECTOR,
                operator=MetadataFilterOperator.EQUAL,
                values=("Local",),
            ),
            (),
            None,
            "local",
        ),
    ],
)
def test_type_source_creator_and_connector_filters_are_exact_normalized(
    clause, observations, source_system, connector
):
    evaluation = _evaluate(
        clause,
        _inspection(*observations, source_system=source_system),
        connector=connector,
    )

    assert evaluation.result == MetadataTruthValue.TRUE


def test_creator_matching_is_not_fuzzy():
    clause = MetadataFilterClause(
        field=MetadataFilterField.CREATOR_OBSERVATION,
        operator=MetadataFilterOperator.EQUAL,
        values=("Alice Smith",),
    )

    evaluation = _evaluate(clause, _inspection(_observation("creator", "Alicia Smith")))

    assert evaluation.result == MetadataTruthValue.FALSE


def test_in_operator_uses_exact_normalized_values():
    clause = MetadataFilterClause(
        field=MetadataFilterField.CREATOR_OBSERVATION,
        operator=MetadataFilterOperator.IN,
        values=("Bob", " ALICE  SMITH "),
    )

    evaluation = _evaluate(clause, _inspection(_observation("creator", "Alice Smith")))

    assert evaluation.result == MetadataTruthValue.TRUE


@pytest.mark.parametrize(
    "operator,values,expected",
    [
        (MetadataFilterOperator.BETWEEN, ("2024-01", "2024-04"), MetadataTruthValue.TRUE),
        (MetadataFilterOperator.BEFORE, ("2024-04",), MetadataTruthValue.TRUE),
        (MetadataFilterOperator.AFTER, ("2024-04",), MetadataTruthValue.FALSE),
    ],
)
def test_temporal_ordering_operators(operator, values, expected):
    inspection = _inspection(_observation("embedded_created_at", "2024-03-10T12:00:00Z"))
    clause = MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=operator,
        values=values,
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
    )

    assert _evaluate(clause, inspection).result == expected


def test_known_false_negation_is_true_but_unknown_negation_remains_unknown():
    inspection = _inspection(_observation("creator", "Alice"))
    clause = MetadataFilterClause(
        field=MetadataFilterField.CREATOR_OBSERVATION,
        operator=MetadataFilterOperator.EQUAL,
        values=("Bob",),
        negated=True,
    )

    assert _evaluate(clause, inspection).result == MetadataTruthValue.TRUE


def test_three_valued_conjunctions_follow_strong_kleene_logic():
    inspection = _inspection(_observation("mime_type", "application/pdf"))
    true_clause = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.EQUAL,
        values=("application/pdf",),
    )
    unknown_clause = _temporal_clause(MetadataFilterField.PRODUCTION_MONTH, "2024-03")
    all_filter = MetadataFilter(clauses=(true_clause, unknown_clause))
    any_filter = MetadataFilter(
        clauses=(true_clause, unknown_clause),
        conjunction=MetadataFilterConjunction.ANY,
    )

    all_result = evaluate_metadata_filter(
        all_filter,
        document_id=inspection.document_id,
        inspection=inspection,
        profile_availability=MetadataProfileAvailability.AVAILABLE,
    )
    any_result = evaluate_metadata_filter(
        any_filter,
        document_id=inspection.document_id,
        inspection=inspection,
        profile_availability=MetadataProfileAvailability.AVAILABLE,
    )

    assert all_result.result == MetadataTruthValue.UNKNOWN
    assert any_result.result == MetadataTruthValue.TRUE


def test_canonical_serialization_is_order_invariant_for_commutative_inputs():
    first = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.IN,
        values=("application/pdf", "text/plain"),
    )
    second = MetadataFilterClause(
        field=MetadataFilterField.SOURCE_SYSTEM,
        operator=MetadataFilterOperator.EQUAL,
        values=("openarchiver",),
    )
    forward = MetadataFilter(clauses=(first, second))
    reverse = MetadataFilter(
        clauses=(second, first.model_copy(update={"values": tuple(reversed(first.values))}))
    )

    assert forward.canonical_json() == reverse.canonical_json()
    assert forward.calculate_sha256() == reverse.calculate_sha256()


def test_temporal_contract_requires_explicit_basis_policy_and_valid_granularity():
    with pytest.raises(ValidationError):
        MetadataFilterClause(
            field=MetadataFilterField.PRODUCTION_MONTH,
            operator=MetadataFilterOperator.EQUAL,
            values=("2024-03",),
        )
    with pytest.raises(ValidationError):
        _temporal_clause(MetadataFilterField.PRODUCTION_MONTH, "2024-13-40")
    with pytest.raises(ValidationError):
        _temporal_clause(MetadataFilterField.PRODUCTION_MONTH, "2024-13")
    with pytest.raises(ValidationError):
        _temporal_clause(MetadataFilterField.PRODUCTION_DAY, "2023-02-29")
