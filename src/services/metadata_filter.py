"""Pure evaluation for ``openrag.metadata-filter v1``.

No query planner, index client, retrieval service, or LLM is imported here.
The caller must first establish the DLS-visible document set.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from models.document_investigation import (
    AssociationDimension,
    CalendarBasis,
    DocumentMetadataInspection,
    InvestigationStatus,
    TemporalSemanticRole,
)
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterBooleanOperator,
    MetadataFilterClause,
    MetadataFilterClauseEvaluation,
    MetadataFilterConjunction,
    MetadataFilterDocumentContext,
    MetadataFilterEvaluation,
    MetadataFilterExpression,
    MetadataFilterField,
    MetadataFilterObservationEvidence,
    MetadataFilterOperator,
    MetadataProfileAvailability,
    MetadataTruthValue,
)


@dataclass(frozen=True)
class _FilterableValue:
    value: str | None
    evidence: MetadataFilterObservationEvidence
    valid: bool


_TEMPORAL_FIELDS: dict[MetadataFilterField, tuple[TemporalSemanticRole, str]] = {
    MetadataFilterField.PRODUCTION_DAY: (TemporalSemanticRole.PRODUCTION, "day"),
    MetadataFilterField.PRODUCTION_MONTH: (TemporalSemanticRole.PRODUCTION, "month"),
    MetadataFilterField.PRODUCTION_YEAR: (TemporalSemanticRole.PRODUCTION, "year"),
    MetadataFilterField.MODIFICATION_DAY: (TemporalSemanticRole.MODIFICATION, "day"),
    MetadataFilterField.MODIFICATION_MONTH: (TemporalSemanticRole.MODIFICATION, "month"),
    MetadataFilterField.MODIFICATION_YEAR: (TemporalSemanticRole.MODIFICATION, "year"),
}

_DIMENSION_FIELDS: dict[MetadataFilterField, AssociationDimension] = {
    MetadataFilterField.MIME: AssociationDimension.SAME_MIME_TYPE,
    MetadataFilterField.FORMAT_FAMILY: AssociationDimension.COMPATIBLE_DOCUMENT_TYPES,
    MetadataFilterField.EXTENSION: AssociationDimension.SAME_EXTENSION,
    MetadataFilterField.SOURCE_DOCUMENT_TYPE: AssociationDimension.SAME_DOCUMENT_TYPE,
    MetadataFilterField.SOURCE_SYSTEM: AssociationDimension.SAME_SOURCE_SYSTEM,
    MetadataFilterField.SOURCE_ENTITY_FAMILY: AssociationDimension.SAME_SOURCE_ENTITY_FAMILY,
    MetadataFilterField.PARENT_COLLECTION: AssociationDimension.SAME_PARENT_COLLECTION,
    MetadataFilterField.CREATOR_OBSERVATION: AssociationDimension.SAME_CREATOR_OBSERVATION,
    MetadataFilterField.LAST_MODIFIER_OBSERVATION: (
        AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION
    ),
    MetadataFilterField.PRODUCER_OBSERVATION: AssociationDimension.SAME_PRODUCER_OBSERVATION,
    MetadataFilterField.CREATOR_APPLICATION_OBSERVATION: (
        AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION
    ),
}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _normalize_query_value(field: MetadataFilterField, value: str) -> str:
    if field == MetadataFilterField.MIME:
        return value.partition(";")[0].strip().lower()
    if field == MetadataFilterField.EXTENSION:
        normalized = value.strip().lower()
        return normalized if normalized.startswith(".") else f".{normalized}"
    if field in {
        MetadataFilterField.PARENT_COLLECTION,
        *set(_TEMPORAL_FIELDS),
    }:
        return value.strip()
    return _normalize_text(value)


def _temporal_values(
    clause: MetadataFilterClause,
    inspection: DocumentMetadataInspection,
) -> list[_FilterableValue]:
    role, granularity = _TEMPORAL_FIELDS[clause.field]
    attribute = (
        f"source_local_{granularity}"
        if clause.calendar_basis == CalendarBasis.SOURCE_LOCAL
        else f"utc_{granularity}"
    )
    values: list[_FilterableValue] = []
    for item in inspection.temporal_observations:
        if item.semantic_role != role:
            continue
        if clause.source_policy == MetadataDateSourcePolicy.EXPLICIT_SOURCE:
            if _normalize_text(item.source) != _normalize_text(clause.explicit_source or ""):
                continue
        value = getattr(item, attribute)
        valid = item.status != InvestigationStatus.INVALID and value is not None
        values.append(
            _FilterableValue(
                value=value,
                valid=valid,
                evidence=MetadataFilterObservationEvidence(
                    observation_id=item.observation_id,
                    field=item.field,
                    source=item.source,
                    normalized_value=value,
                    status=item.status,
                    timezone_status=item.timezone_status,
                ),
            )
        )
    return values


def _dimension_values(
    clause: MetadataFilterClause,
    inspection: DocumentMetadataInspection,
) -> list[_FilterableValue]:
    dimension = _DIMENSION_FIELDS[clause.field]
    return [
        _FilterableValue(
            value=item.value,
            valid=True,
            evidence=MetadataFilterObservationEvidence(
                observation_id=item.observation_id,
                field=item.field,
                source=item.source,
                normalized_value=item.value,
                status=InvestigationStatus.OBSERVED,
            ),
        )
        for item in inspection.association_ready_values
        if item.name == dimension.value
    ]


def _context_values(
    clause: MetadataFilterClause,
    context: MetadataFilterDocumentContext | None,
) -> list[_FilterableValue]:
    if context is None:
        return []
    return [
        _FilterableValue(
            value=_normalize_query_value(item.field, item.value),
            valid=True,
            evidence=MetadataFilterObservationEvidence(
                observation_id=(
                    f"context:{context.document_id}:{item.field.value}:"
                    f"{_normalize_query_value(item.field, item.value)}"
                ),
                field=item.field.value,
                source=item.source,
                normalized_value=_normalize_query_value(item.field, item.value),
                status=InvestigationStatus.ASSERTED,
            ),
        )
        for item in context.values
        if item.field == clause.field
    ]


def truth_not(result: MetadataTruthValue) -> MetadataTruthValue:
    """Strong-Kleene NOT; UNKNOWN never becomes TRUE."""
    return {
        MetadataTruthValue.TRUE: MetadataTruthValue.FALSE,
        MetadataTruthValue.FALSE: MetadataTruthValue.TRUE,
        MetadataTruthValue.UNKNOWN: MetadataTruthValue.UNKNOWN,
    }[result]


def truth_and(values: tuple[MetadataTruthValue, ...]) -> MetadataTruthValue:
    """Strong-Kleene AND over a non-empty tuple."""
    if not values:
        raise ValueError("truth_and requires at least one value")
    if MetadataTruthValue.FALSE in values:
        return MetadataTruthValue.FALSE
    if all(value == MetadataTruthValue.TRUE for value in values):
        return MetadataTruthValue.TRUE
    return MetadataTruthValue.UNKNOWN


def truth_or(values: tuple[MetadataTruthValue, ...]) -> MetadataTruthValue:
    """Strong-Kleene OR over a non-empty tuple."""
    if not values:
        raise ValueError("truth_or requires at least one value")
    if MetadataTruthValue.TRUE in values:
        return MetadataTruthValue.TRUE
    if all(value == MetadataTruthValue.FALSE for value in values):
        return MetadataTruthValue.FALSE
    return MetadataTruthValue.UNKNOWN


def _matches(operator: MetadataFilterOperator, value: str, targets: tuple[str, ...]) -> bool:
    if operator == MetadataFilterOperator.EQUAL:
        return value == targets[0]
    if operator == MetadataFilterOperator.IN:
        return value in targets
    if operator == MetadataFilterOperator.BETWEEN:
        return targets[0] <= value <= targets[1]
    if operator == MetadataFilterOperator.BEFORE:
        return value < targets[0]
    if operator == MetadataFilterOperator.AFTER:
        return value > targets[0]
    raise ValueError(f"operator {operator.value} is not a value comparison")


def _unique_evidence(
    values: list[MetadataFilterObservationEvidence],
) -> tuple[MetadataFilterObservationEvidence, ...]:
    unique = {
        (
            item.observation_id,
            item.field,
            item.source,
            item.normalized_value,
            item.status,
            item.timezone_status,
        ): item
        for item in values
    }
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.observation_id,
                item.field,
                item.source,
                item.normalized_value or "",
            ),
        )
    )


def evaluate_metadata_filter_clause(
    clause: MetadataFilterClause,
    *,
    inspection: DocumentMetadataInspection | None,
    profile_availability: MetadataProfileAvailability,
    context: MetadataFilterDocumentContext | None = None,
) -> MetadataFilterClauseEvaluation:
    """Evaluate one clause using fail-closed strong-Kleene negation."""
    context_candidates = _context_values(clause, context)
    context_complete = context is not None and clause.field in context.complete_fields
    if (
        (profile_availability != MetadataProfileAvailability.AVAILABLE or inspection is None)
        and not context_complete
    ):
        result = MetadataTruthValue.UNKNOWN
        if clause.negated:
            result = truth_not(result)
        return MetadataFilterClauseEvaluation(
            clause_sha256=clause.calculate_sha256(),
            result=result,
        )

    if inspection is None:
        candidates = context_candidates
    elif clause.field in _TEMPORAL_FIELDS:
        candidates = _temporal_values(clause, inspection)
    elif clause.field == MetadataFilterField.CONNECTOR:
        candidates = context_candidates
    else:
        candidates = [*_dimension_values(clause, inspection), *context_candidates]

    valid = [item for item in candidates if item.valid and item.value is not None]
    invalid = [item for item in candidates if not item.valid or item.value is None]
    matched: list[_FilterableValue] = []

    if clause.operator in {MetadataFilterOperator.EXISTS, MetadataFilterOperator.NOT_EXISTS}:
        if valid:
            result = MetadataTruthValue.TRUE
        elif invalid:
            result = MetadataTruthValue.UNKNOWN
        else:
            result = MetadataTruthValue.FALSE
        if clause.operator == MetadataFilterOperator.NOT_EXISTS:
            result = truth_not(result)
        if valid and result == MetadataTruthValue.TRUE:
            matched = valid
    else:
        targets = tuple(_normalize_query_value(clause.field, item) for item in clause.values)
        matched = [
            item for item in valid if _matches(clause.operator, item.value or "", targets)
        ]
        if matched:
            result = MetadataTruthValue.TRUE
        elif valid:
            result = MetadataTruthValue.FALSE
        else:
            result = MetadataTruthValue.UNKNOWN

    distinct_valid_values = {item.value for item in valid}
    conflicting = (
        [item for item in valid if item not in matched]
        if len(distinct_valid_values) > 1
        else []
    )
    if clause.negated:
        result = truth_not(result)
    return MetadataFilterClauseEvaluation(
        clause_sha256=clause.calculate_sha256(),
        result=result,
        matched_observations=_unique_evidence([item.evidence for item in matched]),
        conflicting_observations=_unique_evidence(
            [item.evidence for item in [*conflicting, *invalid]]
        ),
    )


def _combine(
    values: tuple[MetadataTruthValue, ...],
    conjunction: MetadataFilterConjunction,
) -> MetadataTruthValue:
    if conjunction == MetadataFilterConjunction.ALL:
        return truth_and(values)
    return truth_or(values)


def _evaluate_expression(
    expression: MetadataFilterExpression,
    *,
    inspection: DocumentMetadataInspection | None,
    profile_availability: MetadataProfileAvailability,
    context: MetadataFilterDocumentContext | None,
) -> tuple[MetadataTruthValue, tuple[MetadataFilterClauseEvaluation, ...]]:
    if expression.clause is not None:
        evaluation = evaluate_metadata_filter_clause(
            expression.clause,
            inspection=inspection,
            profile_availability=profile_availability,
            context=context,
        )
        return evaluation.result, (evaluation,)
    child_results = tuple(
        _evaluate_expression(
            child,
            inspection=inspection,
            profile_availability=profile_availability,
            context=context,
        )
        for child in expression.children
    )
    values = tuple(item[0] for item in child_results)
    evaluations = tuple(
        evaluation for item in child_results for evaluation in item[1]
    )
    if expression.operator == MetadataFilterBooleanOperator.NOT:
        return truth_not(values[0]), evaluations
    if expression.operator == MetadataFilterBooleanOperator.AND:
        return truth_and(values), evaluations
    if expression.operator == MetadataFilterBooleanOperator.OR:
        return truth_or(values), evaluations
    raise ValueError("invalid metadata filter expression")


def evaluate_metadata_filter(
    metadata_filter: MetadataFilter,
    *,
    document_id: str,
    inspection: DocumentMetadataInspection | None,
    profile_availability: MetadataProfileAvailability,
    context: MetadataFilterDocumentContext | None = None,
) -> MetadataFilterEvaluation:
    """Evaluate a structured filter; only ``evaluation.matches`` is eligible."""
    if inspection is not None and inspection.document_id != document_id:
        raise ValueError("inspection identity does not match document_id")
    if context is not None and context.document_id != document_id:
        raise ValueError("context identity does not match document_id")
    if metadata_filter.expression is not None:
        result, clause_evaluations = _evaluate_expression(
            metadata_filter.expression,
            inspection=inspection,
            profile_availability=profile_availability,
            context=context,
        )
    else:
        clause_evaluations = tuple(
            evaluate_metadata_filter_clause(
                clause,
                inspection=inspection,
                profile_availability=profile_availability,
                context=context,
            )
            for clause in metadata_filter.clauses
        )
        result = _combine(
            tuple(item.result for item in clause_evaluations),
            metadata_filter.conjunction,
        )
    matched = _unique_evidence(
        [
            evidence
            for evaluation in clause_evaluations
            for evidence in evaluation.matched_observations
        ]
    )
    conflicts = _unique_evidence(
        [
            evidence
            for evaluation in clause_evaluations
            for evidence in evaluation.conflicting_observations
        ]
    )
    return MetadataFilterEvaluation(
        document_id=document_id,
        filter_sha256=metadata_filter.calculate_sha256(),
        result=result,
        clause_evaluations=clause_evaluations,
        matched_observations=matched,
        conflicting_observations=conflicts,
        profile_availability=profile_availability,
    )
