"""Generate and query ``openrag.metadata-filter-projection v1``.

This module is deterministic and has no OpenSearch client dependency.  Query
plans explicitly require a DLS-scoped client boundary; production integration
is intentionally left to a later chantier.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from models.document_investigation import (
    AssociationDimension,
    CalendarBasis,
    InvestigationConflictCode,
    InvestigationStatus,
    InvestigationTimezoneStatus,
    TemporalSemanticRole,
)
from models.document_metadata import DocumentMetadataProfile
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterBooleanOperator,
    MetadataFilterClause,
    MetadataFilterConjunction,
    MetadataFilterExpression,
    MetadataFilterField,
    MetadataFilterObservationEvidence,
    MetadataFilterOperator,
    MetadataProjectionFilterEvaluation,
    MetadataTruthValue,
)
from models.metadata_filter_projection import (
    METADATA_FILTER_PROJECTION_FIELD,
    MetadataFilterProjection,
    MetadataFilterProjectionSideDocument,
    MetadataFilterProjectionSourceContext,
    MetadataProjectionObservationSource,
    ProjectedTemporalObservation,
    ProjectedValueObservation,
)
from models.source_provenance import SourceProvenance
from services.document_investigation import inspect_document_metadata
from services.metadata_filter import truth_and, truth_not, truth_or


class MetadataProjectionQueryBoundary(StrEnum):
    DLS_SCOPED_OPENSEARCH_CLIENT = "DLS_SCOPED_OPENSEARCH_CLIENT"


_DIMENSION_TO_FIELD: dict[AssociationDimension, str] = {
    AssociationDimension.SAME_MIME_TYPE: "mime_types",
    AssociationDimension.COMPATIBLE_DOCUMENT_TYPES: "format_families",
    AssociationDimension.SAME_EXTENSION: "extensions",
    AssociationDimension.SAME_DOCUMENT_TYPE: "explicit_document_types",
    AssociationDimension.SAME_SOURCE_ENTITY_FAMILY: "source_entity_families",
    AssociationDimension.SAME_CREATOR_OBSERVATION: "creator_normalized",
    AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION: "last_modifier_normalized",
    AssociationDimension.SAME_PRODUCER_OBSERVATION: "producer_normalized",
    AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION: (
        "creator_application_normalized"
    ),
    AssociationDimension.SAME_FILENAME_BASENAME: "filename_basename_normalized",
    AssociationDimension.SAME_BINARY_HASH: "binary_sha256",
}

_FILTER_TO_PROJECTION: dict[MetadataFilterField, str] = {
    MetadataFilterField.MIME: "mime_types",
    MetadataFilterField.FORMAT_FAMILY: "format_families",
    MetadataFilterField.EXTENSION: "extensions",
    MetadataFilterField.SOURCE_DOCUMENT_TYPE: "explicit_document_types",
    MetadataFilterField.SOURCE_SYSTEM: "source_systems",
    MetadataFilterField.SOURCE_ENTITY_TYPE: "source_entity_types",
    MetadataFilterField.SOURCE_ENTITY_FAMILY: "source_entity_families",
    MetadataFilterField.PARENT_COLLECTION: "parent_collection_ids_safe",
    MetadataFilterField.CONNECTOR: "source_connectors",
    MetadataFilterField.CREATOR_OBSERVATION: "creator_normalized",
    MetadataFilterField.LAST_MODIFIER_OBSERVATION: "last_modifier_normalized",
    MetadataFilterField.PRODUCER_OBSERVATION: "producer_normalized",
    MetadataFilterField.CREATOR_APPLICATION_OBSERVATION: (
        "creator_application_normalized"
    ),
    MetadataFilterField.FILENAME_BASENAME: "filename_basename_normalized",
    MetadataFilterField.BINARY_SHA256: "binary_sha256",
    MetadataFilterField.HAS_TEMPORAL_CONFLICT: "has_temporal_conflict",
    MetadataFilterField.HAS_METADATA_CONFLICT: "has_metadata_conflict",
}

_TEMPORAL_ROLES: dict[MetadataFilterField, tuple[str, str]] = {
    MetadataFilterField.PRODUCTION_DAY: ("production", "day"),
    MetadataFilterField.PRODUCTION_MONTH: ("production", "month"),
    MetadataFilterField.PRODUCTION_YEAR: ("production", "year"),
    MetadataFilterField.MODIFICATION_DAY: ("modification", "day"),
    MetadataFilterField.MODIFICATION_MONTH: ("modification", "month"),
    MetadataFilterField.MODIFICATION_YEAR: ("modification", "year"),
}

_TEMPORAL_CONFLICT_CODES = {
    InvestigationConflictCode.SOURCE_CONFLICT.value,
    InvestigationConflictCode.MULTIPLE_CREATION_OBSERVATIONS.value,
    InvestigationConflictCode.MULTIPLE_MODIFICATION_OBSERVATIONS.value,
    InvestigationConflictCode.MODIFIED_BEFORE_CREATED.value,
    InvestigationConflictCode.ARCHIVE_EMBEDDED_DATE_INVERSION.value,
    InvestigationConflictCode.FUTURE_TIMESTAMP.value,
}


def normalize_filter_text(value: object) -> str:
    """The existing exact NFKC/space/case normalization, without fuzziness."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def _sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def safe_parent_collection_id(value: str) -> str:
    """Make a stable equality key without exposing the parent locator."""
    material = f"openrag.metadata-filter-projection.parent.v1\x00{value}".encode()
    return hashlib.sha256(material).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _source_name(raw_source: str, source_type: str) -> MetadataProjectionObservationSource:
    normalized = normalize_filter_text(raw_source).replace("-", "_").replace(" ", "_")
    exact = {
        "pdf_info_dictionary": MetadataProjectionObservationSource.PDF_INFO,
        "pdf_info": MetadataProjectionObservationSource.PDF_INFO,
        "pdf_xmp": MetadataProjectionObservationSource.PDF_XMP,
        "ooxml_core_properties": MetadataProjectionObservationSource.OOXML_CORE,
        "ooxml_core": MetadataProjectionObservationSource.OOXML_CORE,
        "rfc5322_headers": MetadataProjectionObservationSource.EML_HEADER,
        "eml_header": MetadataProjectionObservationSource.EML_HEADER,
        "image_exif": MetadataProjectionObservationSource.EXIF,
        "exif": MetadataProjectionObservationSource.EXIF,
        "image_xmp": MetadataProjectionObservationSource.XMP,
        "xmp": MetadataProjectionObservationSource.XMP,
        "indexed_document_registry": MetadataProjectionObservationSource.INGESTION,
        "ingestion": MetadataProjectionObservationSource.INGESTION,
    }
    if normalized in exact:
        return exact[normalized]
    if "archive" in normalized or source_type == "archive_native":
        return MetadataProjectionObservationSource.ARCHIVE
    if "filesystem" in normalized or source_type == "filesystem":
        return MetadataProjectionObservationSource.FILESYSTEM
    if source_type == "ingestion":
        return MetadataProjectionObservationSource.INGESTION
    return MetadataProjectionObservationSource.OTHER_FORMAT_NATIVE


def _explicit_source_name(raw_source: str) -> MetadataProjectionObservationSource:
    normalized = normalize_filter_text(raw_source).replace("-", "_").replace(" ", "_")
    result = _source_name(raw_source, "format_native")
    if (
        result == MetadataProjectionObservationSource.OTHER_FORMAT_NATIVE
        and normalized != MetadataProjectionObservationSource.OTHER_FORMAT_NATIVE.value
    ):
        raise ValueError(f"unsupported explicit temporal observation source: {raw_source}")
    return result


def _format_family(mime_type: str) -> str | None:
    value = mime_type.partition(";")[0].strip().lower()
    exact = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
            "text_document"
        ),
        "application/vnd.oasis.opendocument.text": "text_document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
        "application/vnd.oasis.opendocument.spreadsheet": "spreadsheet",
        "application/vnd.ms-excel": "spreadsheet",
        "text/csv": "spreadsheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
            "presentation"
        ),
        "application/vnd.oasis.opendocument.presentation": "presentation",
        "message/rfc822": "email",
        "text/plain": "plain_text",
        "text/markdown": "plain_text",
        "text/html": "html",
    }
    if value.startswith("image/"):
        return "image"
    return exact.get(value)


def _filename_basename(value: str) -> str | None:
    leaf = PurePosixPath(value.replace("\\", "/")).name
    suffix = PurePosixPath(leaf).suffix
    stem = leaf[: -len(suffix)] if suffix else leaf
    normalized = normalize_filter_text(stem)
    return normalized or None


def _extension(value: str) -> str | None:
    suffix = PurePosixPath(value.replace("\\", "/")).suffix.lower()
    return suffix or None


def _source_context_digest(
    context: MetadataFilterProjectionSourceContext,
    provenance: SourceProvenance | None,
) -> str:
    relations: list[dict[str, str]] = []
    if provenance is not None:
        relations = sorted(
            (
                {
                    "role": relation.role.value,
                    "target_id": relation.target.id,
                    "target_type": relation.target.type,
                }
                for relation in provenance.relations
            ),
            key=lambda item: (item["role"], item["target_type"], item["target_id"]),
        )
    return _canonical_sha256(
        {
            "context": context.canonical_payload(),
            "provenance_entity": (
                {
                    "id": provenance.entity.id,
                    "type": provenance.entity.type,
                    "source_system": provenance.entity.source_system,
                }
                if provenance is not None
                else None
            ),
            "provenance_relations": relations,
        }
    )


def generate_metadata_filter_projection(
    profile: DocumentMetadataProfile,
    *,
    source_context: MetadataFilterProjectionSourceContext,
    source_provenance: SourceProvenance | None = None,
) -> MetadataFilterProjection:
    """Project a complete raw profile without selecting preferred observations."""
    if profile.metadata_facts_sha256 != profile.calculate_facts_sha256():
        raise ValueError("source metadata facts digest is stale")
    if source_context.source_entity_id and source_context.source_entity_id != profile.entity_id:
        raise ValueError("source context identity differs from metadata profile")
    if source_provenance is not None and source_provenance.entity.id != profile.entity_id:
        raise ValueError("source provenance identity differs from metadata profile")
    inspection = inspect_document_metadata(profile, source_provenance=source_provenance)
    values: dict[str, set[str]] = {
        field: set() for field in set(_DIMENSION_TO_FIELD.values())
    }
    evidence: list[ProjectedValueObservation] = []
    for ready_item in inspection.association_ready_values:
        dimension = AssociationDimension(ready_item.name)
        projection_field = _DIMENSION_TO_FIELD.get(dimension)
        if projection_field is None:
            continue
        values[projection_field].add(ready_item.value)
        evidence.append(
            ProjectedValueObservation(
                observation_id=ready_item.observation_id,
                field=projection_field,
                source=ready_item.source,
                normalized_value=ready_item.value,
                status=InvestigationStatus.OBSERVED,
            )
        )

    def add_context(field: str, value: str | None, source: str) -> None:
        if value is None or not value.strip():
            return
        normalized = normalize_filter_text(value)
        if field == "mime_types":
            normalized = value.partition(";")[0].strip().lower()
        elif field == "extensions":
            normalized = value.lower()
            if not normalized.startswith("."):
                normalized = f".{normalized}"
        values.setdefault(field, set()).add(normalized)
        evidence.append(
            ProjectedValueObservation(
                observation_id=_canonical_sha256(
                    {
                        "document_id": profile.entity_id,
                        "field": field,
                        "source": source,
                        "value": normalized,
                    }
                ),
                field=field,
                source=source,
                normalized_value=normalized,
                status=InvestigationStatus.ASSERTED,
            )
        )

    add_context("mime_types", source_context.mime_type, "indexed_document.mimetype")
    if source_context.mime_type and (family := _format_family(source_context.mime_type)):
        add_context("format_families", family, "indexed_document.mimetype")
    if source_context.filename:
        if suffix := _extension(source_context.filename):
            add_context("extensions", suffix, "indexed_document.filename")
        if basename := _filename_basename(source_context.filename):
            add_context(
                "filename_basename_normalized",
                basename,
                "indexed_document.filename",
            )
    source_system = (
        source_provenance.entity.source_system if source_provenance is not None else None
    ) or source_context.source_system
    source_type = (
        source_provenance.entity.type if source_provenance is not None else None
    ) or source_context.source_entity_type
    add_context("source_systems", source_system, "source_provenance.entity.source_system")
    add_context("source_entity_types", source_type, "source_provenance.entity.type")
    add_context("source_connectors", source_context.connector, "indexed_document.connector_type")

    parent_ids: set[str] = set()
    if source_provenance is not None:
        for relation in source_provenance.relations:
            if relation.role.value not in {"attachment_of", "member_of", "contained_in"}:
                continue
            safe_id = safe_parent_collection_id(relation.target.id)
            parent_ids.add(safe_id)
            evidence.append(
                ProjectedValueObservation(
                    observation_id=_canonical_sha256(
                        {
                            "document_id": profile.entity_id,
                            "role": relation.role.value,
                            "target_id": relation.target.id,
                        }
                    ),
                    field="parent_collection_ids_safe",
                    source=f"source_provenance.relations.{relation.role.value}",
                    normalized_value=safe_id,
                    status=InvestigationStatus.ASSERTED,
                )
            )

    temporal: list[ProjectedTemporalObservation] = []
    for temporal_item in inspection.temporal_observations:
        if temporal_item.semantic_role not in {
            TemporalSemanticRole.PRODUCTION,
            TemporalSemanticRole.MODIFICATION,
        }:
            continue
        role: Literal["production", "modification"] = (
            "production"
            if temporal_item.semantic_role == TemporalSemanticRole.PRODUCTION
            else "modification"
        )
        temporal.append(
            ProjectedTemporalObservation(
                observation_id=temporal_item.observation_id,
                role=role,
                field=temporal_item.field,
                source=_source_name(temporal_item.source, temporal_item.source_type.value),
                status=temporal_item.status,
                timezone_status=temporal_item.timezone_status,
                source_local_day=temporal_item.source_local_day,
                source_local_month=temporal_item.source_local_month,
                source_local_year=temporal_item.source_local_year,
                utc_day=temporal_item.utc_day,
                utc_month=temporal_item.utc_month,
                utc_year=temporal_item.utc_year,
            )
        )
    temporal.sort(key=lambda item: (item.role, item.observation_id))

    def periods(role: str, attribute: str) -> tuple[str, ...]:
        return _sorted(
            str(value)
            for item in temporal
            if item.role == role and (value := getattr(item, attribute)) is not None
        )

    production = [item for item in temporal if item.role == "production"]
    modification = [item for item in temporal if item.role == "modification"]
    conflict_types = _sorted(item.code.value for item in inspection.conflicts)
    has_metadata_conflict = bool(profile.conflicts) or any(
        item.status == InvestigationStatus.CONFLICTING
        for item in inspection.conflicts
    )
    temporal_profile_conflict = any(
        conflict.field
        in {"embedded_created_at", "embedded_sent_at", "embedded_modified_at"}
        for conflict in profile.conflicts
    )

    return MetadataFilterProjection(
        source_metadata_facts_sha256=profile.metadata_facts_sha256,
        source_context_sha256=_source_context_digest(source_context, source_provenance),
        production_day_local=periods("production", "source_local_day"),
        production_month_local=periods("production", "source_local_month"),
        production_year_local=periods("production", "source_local_year"),
        production_day_utc=periods("production", "utc_day"),
        production_month_utc=periods("production", "utc_month"),
        production_year_utc=periods("production", "utc_year"),
        modification_day_local=periods("modification", "source_local_day"),
        modification_month_local=periods("modification", "source_local_month"),
        modification_year_local=periods("modification", "source_local_year"),
        modification_day_utc=periods("modification", "utc_day"),
        modification_month_utc=periods("modification", "utc_month"),
        modification_year_utc=periods("modification", "utc_year"),
        has_production_observation=bool(production),
        has_valid_production_observation=any(
            item.status != InvestigationStatus.INVALID and item.source_local_day
            for item in production
        ),
        has_modification_observation=bool(modification),
        has_valid_modification_observation=any(
            item.status != InvestigationStatus.INVALID and item.source_local_day
            for item in modification
        ),
        has_timezone_unknown=any(
            item.timezone_status == InvestigationTimezoneStatus.UNKNOWN for item in temporal
        ),
        has_invalid_timestamp=any(
            item.status == InvestigationStatus.INVALID for item in temporal
        ),
        has_temporal_conflict=temporal_profile_conflict
        or any(code in _TEMPORAL_CONFLICT_CODES for code in conflict_types),
        production_observation_sources=_sorted(
            item.source.value for item in production
        ),
        modification_observation_sources=_sorted(
            item.source.value for item in modification
        ),
        temporal_observations=tuple(temporal),
        mime_types=_sorted(values["mime_types"]),
        format_families=_sorted(values["format_families"]),
        extensions=_sorted(values["extensions"]),
        explicit_document_types=_sorted(values["explicit_document_types"]),
        source_systems=_sorted(values.get("source_systems", set())),
        source_entity_types=_sorted(values.get("source_entity_types", set())),
        source_entity_families=_sorted(values["source_entity_families"]),
        source_connectors=_sorted(values.get("source_connectors", set())),
        parent_collection_ids_safe=_sorted(parent_ids),
        creator_normalized=_sorted(values["creator_normalized"]),
        last_modifier_normalized=_sorted(values["last_modifier_normalized"]),
        producer_normalized=_sorted(values["producer_normalized"]),
        creator_application_normalized=_sorted(
            values["creator_application_normalized"]
        ),
        filename_basename_normalized=_sorted(
            values["filename_basename_normalized"]
        ),
        binary_sha256=_sorted(values["binary_sha256"]),
        has_metadata_conflict=has_metadata_conflict,
        conflict_types=conflict_types,
        value_observations=tuple(
            sorted(
                {
                    (item.observation_id, item.field, item.normalized_value): item
                    for item in evidence
                }.values(),
                key=lambda item: (item.field, item.normalized_value, item.observation_id),
            )
        ),
    )


def build_projection_side_document(
    projection: MetadataFilterProjection,
    *,
    source_document_id: str,
    source_entity_id: str,
    representative_chunk_id: str,
    owner: str | None,
    allowed_users: Iterable[str] = (),
    allowed_groups: Iterable[str] = (),
    allowed_principals: Iterable[str] = (),
) -> MetadataFilterProjectionSideDocument:
    projection_document_id = _canonical_sha256(
        {
            "contract": projection.contract,
            "owner": owner,
            "source_document_id": source_document_id,
            "source_entity_id": source_entity_id,
        }
    )
    return MetadataFilterProjectionSideDocument(
        projection_document_id=projection_document_id,
        source_document_id=source_document_id,
        source_entity_id=source_entity_id,
        representative_chunk_id=representative_chunk_id,
        owner=owner,
        allowed_users=_sorted(allowed_users),
        allowed_groups=_sorted(allowed_groups),
        allowed_principals=_sorted(allowed_principals),
        filter=projection,
    )


def _normalize_query_value(field: MetadataFilterField, value: str) -> str:
    if field == MetadataFilterField.MIME:
        return value.partition(";")[0].strip().lower()
    if field == MetadataFilterField.EXTENSION:
        normalized = value.strip().lower()
        return normalized if normalized.startswith(".") else f".{normalized}"
    if field == MetadataFilterField.PARENT_COLLECTION:
        return safe_parent_collection_id(value.strip())
    if field == MetadataFilterField.BINARY_SHA256:
        return value.strip().lower()
    if field in _TEMPORAL_ROLES:
        return value.strip()
    if field in {
        MetadataFilterField.HAS_TEMPORAL_CONFLICT,
        MetadataFilterField.HAS_METADATA_CONFLICT,
    }:
        normalized = value.strip().lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{field.value} only accepts true or false")
        return normalized
    return normalize_filter_text(value)


def _value_query(path: str, operator: MetadataFilterOperator, values: tuple[str, ...]) -> dict:
    if operator == MetadataFilterOperator.EQUAL:
        return {"term": {path: values[0]}}
    if operator == MetadataFilterOperator.IN:
        return {"terms": {path: list(values)}}
    if operator == MetadataFilterOperator.BETWEEN:
        return {"range": {path: {"gte": values[0], "lte": values[1]}}}
    if operator == MetadataFilterOperator.BEFORE:
        return {"range": {path: {"lt": values[0]}}}
    if operator == MetadataFilterOperator.AFTER:
        return {"range": {path: {"gt": values[0]}}}
    raise ValueError(f"{operator.value} is not a value comparison")


def _must(queries: Iterable[dict]) -> dict:
    return {"bool": {"filter": list(queries)}}


def _should(queries: Iterable[dict]) -> dict:
    return {"bool": {"should": list(queries), "minimum_should_match": 1}}


def _must_not(query: dict) -> dict:
    return {"bool": {"must_not": [query]}}


def _temporal_clause_queries(clause: MetadataFilterClause) -> tuple[dict, dict]:
    role, granularity = _TEMPORAL_ROLES[clause.field]
    basis = "source_local" if clause.calendar_basis == CalendarBasis.SOURCE_LOCAL else "utc"
    flat_path = f"{METADATA_FILTER_PROJECTION_FIELD}.{role}_{granularity}_{'local' if basis == 'source_local' else 'utc'}"
    values = tuple(_normalize_query_value(clause.field, value) for value in clause.values)
    any_observation = {
        "term": {f"{METADATA_FILTER_PROJECTION_FIELD}.has_{role}_observation": True}
    }

    if clause.source_policy == MetadataDateSourcePolicy.EXPLICIT_SOURCE:
        source = _explicit_source_name(clause.explicit_source or "").value
        nested_prefix = f"{METADATA_FILTER_PROJECTION_FIELD}.temporal_observations"
        source_filter = [
            {"term": {f"{nested_prefix}.role": role}},
            {"term": {f"{nested_prefix}.source": source}},
        ]
        any_for_source = {
            "nested": {
                "path": nested_prefix,
                "query": _must(source_filter),
            }
        }
        value_path = f"{nested_prefix}.{basis}_{granularity}"
        usable = {
            "nested": {
                "path": nested_prefix,
                "query": _must([*source_filter, {"exists": {"field": value_path}}]),
            }
        }
        if clause.operator in {MetadataFilterOperator.EXISTS, MetadataFilterOperator.NOT_EXISTS}:
            true_query, false_query = usable, _must_not(any_for_source)
            if clause.operator == MetadataFilterOperator.NOT_EXISTS:
                true_query, false_query = false_query, true_query
        else:
            match = {
                "nested": {
                    "path": nested_prefix,
                    "query": _must(
                        [*source_filter, _value_query(value_path, clause.operator, values)]
                    ),
                }
            }
            true_query = match
            false_query = _must([usable, _must_not(match)])
    else:
        usable = {"exists": {"field": flat_path}}
        if clause.operator in {MetadataFilterOperator.EXISTS, MetadataFilterOperator.NOT_EXISTS}:
            true_query, false_query = usable, _must_not(any_observation)
            if clause.operator == MetadataFilterOperator.NOT_EXISTS:
                true_query, false_query = false_query, true_query
        else:
            match = _value_query(flat_path, clause.operator, values)
            true_query = match
            false_query = _must([usable, _must_not(match)])
    return (false_query, true_query) if clause.negated else (true_query, false_query)


def _clause_queries(clause: MetadataFilterClause) -> tuple[dict, dict]:
    """Return disjoint OpenSearch predicates for TRUE and FALSE; neither is UNKNOWN."""
    if clause.field in _TEMPORAL_ROLES:
        return _temporal_clause_queries(clause)
    field = _FILTER_TO_PROJECTION[clause.field]
    path = f"{METADATA_FILTER_PROJECTION_FIELD}.{field}"
    exists: dict[str, Any] = {"exists": {"field": path}}
    true_query: dict[str, Any]
    false_query: dict[str, Any]
    if clause.operator in {MetadataFilterOperator.EXISTS, MetadataFilterOperator.NOT_EXISTS}:
        true_query, false_query = exists, _must_not(exists)
        if clause.operator == MetadataFilterOperator.NOT_EXISTS:
            true_query, false_query = false_query, true_query
    else:
        values = tuple(_normalize_query_value(clause.field, value) for value in clause.values)
        if field in {"has_temporal_conflict", "has_metadata_conflict"}:
            boolean_values = tuple(value == "true" for value in values)
            if clause.operator == MetadataFilterOperator.EQUAL:
                match: dict[str, Any] = {"term": {path: boolean_values[0]}}
            elif clause.operator == MetadataFilterOperator.IN:
                match = {"terms": {path: list(boolean_values)}}
            else:
                raise ValueError("boolean projection fields only support EQUAL and IN")
        else:
            match = _value_query(path, clause.operator, values)
        true_query = match
        false_query = _must([exists, _must_not(match)])
    return (false_query, true_query) if clause.negated else (true_query, false_query)


def _expression_queries(expression: MetadataFilterExpression) -> tuple[dict, dict]:
    if expression.clause is not None:
        return _clause_queries(expression.clause)
    children = tuple(_expression_queries(child) for child in expression.children)
    if expression.operator == MetadataFilterBooleanOperator.NOT:
        true_query, false_query = children[0]
        return false_query, true_query
    if expression.operator == MetadataFilterBooleanOperator.AND:
        return _must(item[0] for item in children), _should(item[1] for item in children)
    if expression.operator == MetadataFilterBooleanOperator.OR:
        return _should(item[0] for item in children), _must(item[1] for item in children)
    raise ValueError("invalid metadata filter expression")


def compile_metadata_filter_to_opensearch(
    metadata_filter: MetadataFilter,
    *,
    boundary: MetadataProjectionQueryBoundary,
) -> dict[str, Any]:
    """Compile only the TRUE set and fail if the DLS boundary is not explicit."""
    if boundary != MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT:
        raise ValueError("metadata projection queries require a DLS-scoped OpenSearch client")
    projection_exists = {
        "exists": {"field": f"{METADATA_FILTER_PROJECTION_FIELD}.projection_sha256"}
    }
    if metadata_filter.expression is not None:
        true_query, _ = _expression_queries(metadata_filter.expression)
    else:
        clauses = tuple(_clause_queries(clause) for clause in metadata_filter.clauses)
        true_query = (
            _must(item[0] for item in clauses)
            if metadata_filter.conjunction == MetadataFilterConjunction.ALL
            else _should(item[0] for item in clauses)
        )
    return _must([projection_exists, true_query])


def _projection_candidates(
    projection: MetadataFilterProjection,
    clause: MetadataFilterClause,
) -> tuple[list[tuple[str, MetadataFilterObservationEvidence]], bool]:
    if clause.field in _TEMPORAL_ROLES:
        role, granularity = _TEMPORAL_ROLES[clause.field]
        attribute = (
            f"source_local_{granularity}"
            if clause.calendar_basis == CalendarBasis.SOURCE_LOCAL
            else f"utc_{granularity}"
        )
        explicit_source = (
            _explicit_source_name(clause.explicit_source or "").value
            if clause.source_policy == MetadataDateSourcePolicy.EXPLICIT_SOURCE
            else None
        )
        selected = [
            item
            for item in projection.temporal_observations
            if item.role == role
            and (explicit_source is None or item.source.value == explicit_source)
        ]
        result = []
        for item in selected:
            value = getattr(item, attribute)
            if value is None:
                continue
            result.append(
                (
                    value,
                    MetadataFilterObservationEvidence(
                        observation_id=item.observation_id,
                        field=item.field,
                        source=item.source.value,
                        normalized_value=value,
                        status=item.status,
                        timezone_status=item.timezone_status,
                    ),
                )
            )
        return result, bool(selected)
    projection_field = _FILTER_TO_PROJECTION[clause.field]
    raw_values = getattr(projection, projection_field)
    if isinstance(raw_values, bool):
        raw_values = (str(raw_values).lower(),)
    evidence_by_value = {
        item.normalized_value: item
        for item in projection.value_observations
        if item.field == projection_field
    }
    result = []
    for value in raw_values:
        value_evidence = evidence_by_value.get(value)
        result.append(
            (
                value,
                MetadataFilterObservationEvidence(
                    observation_id=(
                        value_evidence.observation_id
                        if value_evidence
                        else _canonical_sha256(
                            {"field": projection_field, "value": value}
                        )
                    ),
                    field=projection_field,
                    source=(
                        value_evidence.source
                        if value_evidence
                        else "metadata_filter_projection"
                    ),
                    normalized_value=value,
                    status=(
                        value_evidence.status
                        if value_evidence
                        else InvestigationStatus.ASSERTED
                    ),
                ),
            )
        )
    return result, False


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
    raise ValueError(f"{operator.value} is not a comparison")


def _evaluate_projection_clause(
    projection: MetadataFilterProjection,
    clause: MetadataFilterClause,
) -> tuple[MetadataTruthValue, tuple[MetadataFilterObservationEvidence, ...], str]:
    candidates, has_unusable = _projection_candidates(projection, clause)
    matched: list[MetadataFilterObservationEvidence] = []
    if clause.operator in {MetadataFilterOperator.EXISTS, MetadataFilterOperator.NOT_EXISTS}:
        if candidates:
            result = MetadataTruthValue.TRUE
            matched = [item[1] for item in candidates]
        elif has_unusable:
            result = MetadataTruthValue.UNKNOWN
        else:
            result = MetadataTruthValue.FALSE
        if clause.operator == MetadataFilterOperator.NOT_EXISTS:
            result = truth_not(result)
    else:
        targets = tuple(_normalize_query_value(clause.field, value) for value in clause.values)
        matched = [item[1] for item in candidates if _matches(clause.operator, item[0], targets)]
        if matched:
            result = MetadataTruthValue.TRUE
        elif candidates:
            result = MetadataTruthValue.FALSE
        elif has_unusable or clause.field in _TEMPORAL_ROLES:
            result = MetadataTruthValue.UNKNOWN
        else:
            result = MetadataTruthValue.FALSE
    if clause.negated:
        result = truth_not(result)
    return result, tuple(matched), (
        _TEMPORAL_ROLES[clause.field][0] + "_" + _TEMPORAL_ROLES[clause.field][1]
        if clause.field in _TEMPORAL_ROLES
        else _FILTER_TO_PROJECTION[clause.field]
    )


def _evaluate_projection_expression(
    projection: MetadataFilterProjection,
    expression: MetadataFilterExpression,
) -> tuple[MetadataTruthValue, list[MetadataFilterObservationEvidence], set[str]]:
    if expression.clause is not None:
        result, evidence, field = _evaluate_projection_clause(projection, expression.clause)
        return result, list(evidence), {field} if result == MetadataTruthValue.TRUE else set()
    children = [
        _evaluate_projection_expression(projection, child) for child in expression.children
    ]
    values = tuple(item[0] for item in children)
    if expression.operator == MetadataFilterBooleanOperator.NOT:
        result = truth_not(values[0])
    elif expression.operator == MetadataFilterBooleanOperator.AND:
        result = truth_and(values)
    elif expression.operator == MetadataFilterBooleanOperator.OR:
        result = truth_or(values)
    else:
        raise ValueError("invalid metadata filter expression")
    return (
        result,
        [evidence for item in children for evidence in item[1]],
        {field for item in children for field in item[2]},
    )


def evaluate_metadata_filter_projection(
    metadata_filter: MetadataFilter,
    *,
    document_id: str,
    projection: MetadataFilterProjection | None,
) -> MetadataProjectionFilterEvaluation:
    """Evaluate stored projection evidence; a missing projection is UNKNOWN."""
    if projection is None:
        return MetadataProjectionFilterEvaluation(
            document_id=document_id,
            filter_sha256=metadata_filter.calculate_sha256(),
            result=MetadataTruthValue.UNKNOWN,
        )
    if metadata_filter.expression is not None:
        result, evidence, fields = _evaluate_projection_expression(
            projection, metadata_filter.expression
        )
    else:
        clauses = [
            _evaluate_projection_clause(projection, clause)
            for clause in metadata_filter.clauses
        ]
        values = tuple(item[0] for item in clauses)
        result = (
            truth_and(values)
            if metadata_filter.conjunction == MetadataFilterConjunction.ALL
            else truth_or(values)
        )
        evidence = [evidence for item in clauses for evidence in item[1]]
        fields = {
            item[2] for item in clauses if item[0] == MetadataTruthValue.TRUE
        }
    unique_evidence = {
        (item.observation_id, item.field, item.normalized_value): item for item in evidence
    }
    return MetadataProjectionFilterEvaluation(
        document_id=document_id,
        filter_sha256=metadata_filter.calculate_sha256(),
        result=result,
        matched_fields=tuple(sorted(fields)),
        matched_observations=tuple(
            sorted(
                unique_evidence.values(),
                key=lambda item: (item.field, item.observation_id),
            )
        ),
        conflict_flags=projection.conflict_types,
        projection_version=projection.projection_version,
    )
