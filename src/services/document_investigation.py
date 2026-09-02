"""Pure document-metadata investigation, chronology, and association primitives.

This module has no OpenSearch, retrieval, connector, API, or LLM dependency.
It deliberately keeps documentary associations outside the PROV-O traversal
policy and filters DLS-inaccessible records before candidate bucketing.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from itertools import combinations, product
from pathlib import PurePosixPath
from typing import Any

from models.document_investigation import (
    AssociationCandidateKey,
    AssociationDimension,
    AssociationDimensionResult,
    AssociationEvidence,
    AssociationReadyValue,
    AssociationStrength,
    AssociationValueSensitivity,
    CalendarBasis,
    CalendarGranularity,
    CalendarMatchStatus,
    CalendarPeriodAssociation,
    CandidateLineageEvidence,
    DocumentaryNeighborhood,
    DocumentAssociation,
    DocumentAssociationEvidenceProjection,
    DocumentChronology,
    DocumentMetadataComparison,
    DocumentMetadataEvidenceProjection,
    DocumentMetadataInspection,
    DocumentTemporalObservation,
    InvestigationConflictCode,
    InvestigationMetadataConflict,
    InvestigationStatus,
    InvestigationTimezoneStatus,
    NeighborhoodInclusion,
    NeighborhoodLimits,
    SafeDocumentProvenance,
    SafeProvenanceRelation,
    TemporalRelation,
    TemporalRelationKind,
    TemporalSemanticRole,
)
from models.document_metadata import (
    DocumentMetadataProfile,
    MetadataNormalizationStatus,
    MetadataObservation,
)
from models.source_provenance import SourceProvenance

_TEMPORAL_FIELDS: dict[str, TemporalSemanticRole] = {
    "embedded_created_at": TemporalSemanticRole.PRODUCTION,
    "embedded_sent_at": TemporalSemanticRole.PRODUCTION,
    "embedded_modified_at": TemporalSemanticRole.MODIFICATION,
    "embedded_digitized_at": TemporalSemanticRole.DIGITIZATION,
    "filesystem_birthtime": TemporalSemanticRole.FILESYSTEM_BIRTHTIME,
    "filesystem_mtime": TemporalSemanticRole.FILESYSTEM_MODIFICATION,
    "filesystem_ctime": TemporalSemanticRole.FILESYSTEM_CHANGE,
    "archived_at": TemporalSemanticRole.ARCHIVED,
    "archive_created_at": TemporalSemanticRole.ARCHIVE_CREATION,
    "archive_modified_at": TemporalSemanticRole.ARCHIVE_MODIFICATION,
    "ingested_at": TemporalSemanticRole.INGESTION,
}

ASSOCIATION_DIMENSION_PRIORITY: tuple[AssociationDimension, ...] = (
    AssociationDimension.SAME_BINARY_HASH,
    AssociationDimension.SAME_PARENT_COLLECTION,
    AssociationDimension.SAME_SOURCE_ENTITY_FAMILY,
    AssociationDimension.SAME_PRODUCTION_INSTANT,
    AssociationDimension.SAME_MODIFICATION_INSTANT,
    AssociationDimension.SAME_PRODUCTION_DAY,
    AssociationDimension.SAME_MODIFICATION_DAY,
    AssociationDimension.SAME_PRODUCTION_DAY_UTC,
    AssociationDimension.SAME_MODIFICATION_DAY_UTC,
    AssociationDimension.SAME_PRODUCTION_MONTH,
    AssociationDimension.SAME_MODIFICATION_MONTH,
    AssociationDimension.SAME_PRODUCTION_MONTH_UTC,
    AssociationDimension.SAME_MODIFICATION_MONTH_UTC,
    AssociationDimension.SAME_CREATOR_OBSERVATION,
    AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION,
    AssociationDimension.SAME_PRODUCER_OBSERVATION,
    AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION,
    AssociationDimension.SAME_DOCUMENT_TYPE,
    AssociationDimension.COMPATIBLE_DOCUMENT_TYPES,
    AssociationDimension.SAME_SOURCE_SYSTEM,
    AssociationDimension.SAME_MIME_TYPE,
    AssociationDimension.SAME_FILENAME_BASENAME,
    AssociationDimension.SAME_EXTENSION,
    AssociationDimension.SAME_PRODUCTION_YEAR,
    AssociationDimension.SAME_MODIFICATION_YEAR,
    AssociationDimension.SAME_PRODUCTION_YEAR_UTC,
    AssociationDimension.SAME_MODIFICATION_YEAR_UTC,
)
_DIMENSION_PRIORITY = {
    dimension: index for index, dimension in enumerate(ASSOCIATION_DIMENSION_PRIORITY)
}

_LOCAL_TEMPORAL_DIMENSIONS: dict[
    tuple[TemporalSemanticRole, CalendarGranularity], AssociationDimension
] = {
    (TemporalSemanticRole.PRODUCTION, CalendarGranularity.DAY): (
        AssociationDimension.SAME_PRODUCTION_DAY
    ),
    (TemporalSemanticRole.PRODUCTION, CalendarGranularity.MONTH): (
        AssociationDimension.SAME_PRODUCTION_MONTH
    ),
    (TemporalSemanticRole.PRODUCTION, CalendarGranularity.YEAR): (
        AssociationDimension.SAME_PRODUCTION_YEAR
    ),
    (TemporalSemanticRole.MODIFICATION, CalendarGranularity.DAY): (
        AssociationDimension.SAME_MODIFICATION_DAY
    ),
    (TemporalSemanticRole.MODIFICATION, CalendarGranularity.MONTH): (
        AssociationDimension.SAME_MODIFICATION_MONTH
    ),
    (TemporalSemanticRole.MODIFICATION, CalendarGranularity.YEAR): (
        AssociationDimension.SAME_MODIFICATION_YEAR
    ),
}
_UTC_TEMPORAL_DIMENSIONS: dict[
    tuple[TemporalSemanticRole, CalendarGranularity], AssociationDimension
] = {
    (TemporalSemanticRole.PRODUCTION, CalendarGranularity.DAY): (
        AssociationDimension.SAME_PRODUCTION_DAY_UTC
    ),
    (TemporalSemanticRole.PRODUCTION, CalendarGranularity.MONTH): (
        AssociationDimension.SAME_PRODUCTION_MONTH_UTC
    ),
    (TemporalSemanticRole.PRODUCTION, CalendarGranularity.YEAR): (
        AssociationDimension.SAME_PRODUCTION_YEAR_UTC
    ),
    (TemporalSemanticRole.MODIFICATION, CalendarGranularity.DAY): (
        AssociationDimension.SAME_MODIFICATION_DAY_UTC
    ),
    (TemporalSemanticRole.MODIFICATION, CalendarGranularity.MONTH): (
        AssociationDimension.SAME_MODIFICATION_MONTH_UTC
    ),
    (TemporalSemanticRole.MODIFICATION, CalendarGranularity.YEAR): (
        AssociationDimension.SAME_MODIFICATION_YEAR_UTC
    ),
}
_INSTANT_DIMENSIONS = {
    TemporalSemanticRole.PRODUCTION: AssociationDimension.SAME_PRODUCTION_INSTANT,
    TemporalSemanticRole.MODIFICATION: AssociationDimension.SAME_MODIFICATION_INSTANT,
}
_CALENDAR_DIMENSION_BASIS = {
    **{dimension: CalendarBasis.SOURCE_LOCAL for dimension in _LOCAL_TEMPORAL_DIMENSIONS.values()},
    **{dimension: CalendarBasis.UTC for dimension in _UTC_TEMPORAL_DIMENSIONS.values()},
}

_PARENT_COLLECTION_ROLES = {"attachment_of", "member_of", "contained_in"}
_CREATOR_FIELDS = {"creator", "author"}
_DOCUMENT_TYPE_FIELDS = {"documentary_type", "source_declared_type"}
_SOURCE_FAMILY_FIELDS = {"source_entity_family", "source_occurrence_family"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def _values(value: object) -> tuple[object, ...]:
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _observation_id(document_id: str, observation: MetadataObservation) -> str:
    return _sha256({"document_id": document_id, "fact": observation.canonical_fact()})


def _synthetic_observation_id(document_id: str, source: str, field: str, value: str) -> str:
    return _sha256(
        {"document_id": document_id, "source": source, "field": field, "value": value}
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None


def _temporal_observation(
    document_id: str,
    observation: MetadataObservation,
) -> DocumentTemporalObservation | None:
    role = _TEMPORAL_FIELDS.get(observation.field)
    if role is None:
        return None
    normalized = str(observation.value) if observation.value is not None else None
    parsed = _parse_datetime(normalized)
    explicitly_invalid = observation.normalization_status == MetadataNormalizationStatus.INVALID
    if explicitly_invalid or parsed is None:
        timezone_status = InvestigationTimezoneStatus.INVALID
        status = InvestigationStatus.INVALID
        normalized = None
        parsed = None
    elif observation.normalization_status == MetadataNormalizationStatus.TIMEZONE_UNKNOWN:
        timezone_status = InvestigationTimezoneStatus.UNKNOWN
        status = InvestigationStatus.OBSERVED
    elif parsed.tzinfo is not None and parsed.utcoffset() is not None:
        timezone_status = InvestigationTimezoneStatus.EXPLICIT_OFFSET
        status = InvestigationStatus.OBSERVED
    else:
        timezone_status = InvestigationTimezoneStatus.UNKNOWN
        status = InvestigationStatus.OBSERVED

    aware = parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None
    utc_value = parsed.astimezone(UTC) if aware and parsed is not None else None
    return DocumentTemporalObservation(
        observation_id=_observation_id(document_id, observation),
        document_id=document_id,
        semantic_role=role,
        field=observation.field,
        raw_value=observation.raw_value,
        normalized_value=normalized,
        timezone_status=timezone_status,
        timezone=observation.timezone,
        source=observation.source,
        source_type=observation.source_type,
        trust_class=observation.trust_class,
        status=status,
        extracted_at=observation.extracted_at,
        normalization_version=observation.normalization_version,
        instant_utc=utc_value.isoformat().replace("+00:00", "Z") if utc_value else None,
        source_local_day=parsed.strftime("%Y-%m-%d") if parsed else None,
        source_local_month=parsed.strftime("%Y-%m") if parsed else None,
        source_local_year=parsed.strftime("%Y") if parsed else None,
        utc_day=utc_value.strftime("%Y-%m-%d") if utc_value else None,
        utc_month=utc_value.strftime("%Y-%m") if utc_value else None,
        utc_year=utc_value.strftime("%Y") if utc_value else None,
    )


def compare_temporal_observations(
    left: DocumentTemporalObservation,
    right: DocumentTemporalObservation,
) -> TemporalRelation:
    """Compare only verified UTC instants; timezone uncertainty is indeterminate."""
    left_instant = _parse_datetime(left.instant_utc)
    right_instant = _parse_datetime(right.instant_utc)
    if left_instant is None or right_instant is None:
        relation = TemporalRelationKind.INDETERMINATE
    elif left_instant < right_instant:
        relation = TemporalRelationKind.BEFORE
    elif left_instant > right_instant:
        relation = TemporalRelationKind.AFTER
    else:
        relation = TemporalRelationKind.EQUAL
    return TemporalRelation(
        left_document_id=left.document_id,
        right_document_id=right.document_id,
        left_observation_id=left.observation_id,
        right_observation_id=right.observation_id,
        relation=relation,
    )


def compare_calendar_periods(
    left: DocumentTemporalObservation,
    right: DocumentTemporalObservation,
    *,
    basis: CalendarBasis,
    granularity: CalendarGranularity,
) -> CalendarPeriodAssociation:
    if left.semantic_role != right.semantic_role:
        raise ValueError("calendar-period comparison requires the same semantic role")
    attribute = {
        (CalendarBasis.SOURCE_LOCAL, CalendarGranularity.DAY): "source_local_day",
        (CalendarBasis.SOURCE_LOCAL, CalendarGranularity.MONTH): "source_local_month",
        (CalendarBasis.SOURCE_LOCAL, CalendarGranularity.YEAR): "source_local_year",
        (CalendarBasis.UTC, CalendarGranularity.DAY): "utc_day",
        (CalendarBasis.UTC, CalendarGranularity.MONTH): "utc_month",
        (CalendarBasis.UTC, CalendarGranularity.YEAR): "utc_year",
    }[(basis, granularity)]
    left_period = getattr(left, attribute)
    right_period = getattr(right, attribute)
    if left_period is None or right_period is None:
        status = CalendarMatchStatus.INDETERMINATE
    elif left_period == right_period:
        status = CalendarMatchStatus.MATCH
    else:
        status = CalendarMatchStatus.DIFFERENT
    return CalendarPeriodAssociation(
        left_document_id=left.document_id,
        right_document_id=right.document_id,
        left_observation_id=left.observation_id,
        right_observation_id=right.observation_id,
        semantic_role=left.semantic_role,
        basis=basis,
        granularity=granularity,
        status=status,
        left_period=left_period,
        right_period=right_period,
        normalized_period=left_period if status == CalendarMatchStatus.MATCH else None,
    )


def _safe_provenance(
    document_id: str,
    provenance: SourceProvenance | None,
) -> SafeDocumentProvenance:
    if provenance is None:
        return SafeDocumentProvenance(document_id=document_id)
    relations = sorted(
        (
            SafeProvenanceRelation(
                role=relation.role.value,
                target_id=relation.target.id,
                target_type=relation.target.type,
            )
            for relation in provenance.relations
        ),
        key=lambda item: (item.role, item.target_type, item.target_id),
    )
    return SafeDocumentProvenance(
        document_id=document_id,
        source_entity_type=provenance.entity.type,
        source_system=provenance.entity.source_system,
        asserted_relations=relations,
    )


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
    if value in exact:
        return exact[value]
    if value.startswith("image/"):
        return "image"
    return None


def _filename_basename(value: str) -> str | None:
    leaf = PurePosixPath(value.replace("\\", "/")).name
    if not leaf:
        return None
    suffix = PurePosixPath(leaf).suffix
    basename = leaf[: -len(suffix)] if suffix else leaf
    normalized = _normalize_text(basename)
    return normalized or None


def _normalized_observation_values(
    observation: MetadataObservation,
) -> list[tuple[AssociationDimension, str, AssociationValueSensitivity]]:
    results: list[tuple[AssociationDimension, str, AssociationValueSensitivity]] = []
    for raw_value in _values(observation.value):
        if raw_value is None:
            continue
        text = str(raw_value).strip()
        if not text:
            continue
        if observation.field in _CREATOR_FIELDS:
            results.append(
                (
                    AssociationDimension.SAME_CREATOR_OBSERVATION,
                    _normalize_text(text),
                    AssociationValueSensitivity.SENSITIVE,
                )
            )
        elif observation.field == "last_modified_by":
            results.append(
                (
                    AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION,
                    _normalize_text(text),
                    AssociationValueSensitivity.SENSITIVE,
                )
            )
        elif observation.field == "producer":
            results.append(
                (
                    AssociationDimension.SAME_PRODUCER_OBSERVATION,
                    _normalize_text(text),
                    AssociationValueSensitivity.INTERNAL,
                )
            )
        elif observation.field == "creator_application":
            results.append(
                (
                    AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION,
                    _normalize_text(text),
                    AssociationValueSensitivity.INTERNAL,
                )
            )
        elif observation.field == "mime_type":
            mime_type = text.partition(";")[0].strip().lower()
            results.append(
                (
                    AssociationDimension.SAME_MIME_TYPE,
                    mime_type,
                    AssociationValueSensitivity.NON_SENSITIVE,
                )
            )
            if family := _format_family(mime_type):
                results.append(
                    (
                        AssociationDimension.COMPATIBLE_DOCUMENT_TYPES,
                        family,
                        AssociationValueSensitivity.NON_SENSITIVE,
                    )
                )
        elif observation.field == "extension":
            extension = text.lower()
            if not extension.startswith("."):
                extension = f".{extension}"
            results.append(
                (
                    AssociationDimension.SAME_EXTENSION,
                    extension,
                    AssociationValueSensitivity.NON_SENSITIVE,
                )
            )
        elif observation.field in {"original_filename", "archive_original_name"}:
            if basename := _filename_basename(text):
                results.append(
                    (
                        AssociationDimension.SAME_FILENAME_BASENAME,
                        basename,
                        AssociationValueSensitivity.SENSITIVE,
                    )
                )
        elif observation.field == "sha256":
            results.append(
                (
                    AssociationDimension.SAME_BINARY_HASH,
                    text.lower(),
                    AssociationValueSensitivity.INTERNAL,
                )
            )
        elif observation.field in _DOCUMENT_TYPE_FIELDS:
            results.append(
                (
                    AssociationDimension.SAME_DOCUMENT_TYPE,
                    _normalize_text(text),
                    AssociationValueSensitivity.INTERNAL,
                )
            )
        elif observation.field in _SOURCE_FAMILY_FIELDS:
            results.append(
                (
                    AssociationDimension.SAME_SOURCE_ENTITY_FAMILY,
                    _normalize_text(text),
                    AssociationValueSensitivity.INTERNAL,
                )
            )
        elif observation.field == "archive_source":
            results.append(
                (
                    AssociationDimension.SAME_SOURCE_SYSTEM,
                    _normalize_text(text),
                    AssociationValueSensitivity.INTERNAL,
                )
            )
    return results


def _temporal_ready_values(
    observation: DocumentTemporalObservation,
) -> Iterable[tuple[AssociationDimension, str]]:
    if observation.semantic_role not in {
        TemporalSemanticRole.PRODUCTION,
        TemporalSemanticRole.MODIFICATION,
    }:
        return ()
    results: list[tuple[AssociationDimension, str]] = []
    instant_dimension = _INSTANT_DIMENSIONS[observation.semantic_role]
    if observation.instant_utc:
        results.append((instant_dimension, observation.instant_utc))
    for granularity, attribute in (
        (CalendarGranularity.DAY, "source_local_day"),
        (CalendarGranularity.MONTH, "source_local_month"),
        (CalendarGranularity.YEAR, "source_local_year"),
    ):
        if value := getattr(observation, attribute):
            results.append(
                (_LOCAL_TEMPORAL_DIMENSIONS[(observation.semantic_role, granularity)], value)
            )
    for granularity, attribute in (
        (CalendarGranularity.DAY, "utc_day"),
        (CalendarGranularity.MONTH, "utc_month"),
        (CalendarGranularity.YEAR, "utc_year"),
    ):
        if value := getattr(observation, attribute):
            results.append(
                (_UTC_TEMPORAL_DIMENSIONS[(observation.semantic_role, granularity)], value)
            )
    return results


def _association_ready_values(
    document_id: str,
    observations: Sequence[MetadataObservation],
    temporal: Sequence[DocumentTemporalObservation],
    provenance: SafeDocumentProvenance,
) -> list[AssociationReadyValue]:
    values: list[AssociationReadyValue] = []
    for observation in observations:
        observation_id = _observation_id(document_id, observation)
        for dimension, value, sensitivity in _normalized_observation_values(observation):
            values.append(
                AssociationReadyValue(
                    name=dimension.value,
                    value=value,
                    observation_id=observation_id,
                    field=observation.field,
                    source=observation.source,
                    sensitivity=sensitivity,
                )
            )
    for temporal_observation in temporal:
        for dimension, value in _temporal_ready_values(temporal_observation):
            values.append(
                AssociationReadyValue(
                    name=dimension.value,
                    value=value,
                    observation_id=temporal_observation.observation_id,
                    field=temporal_observation.field,
                    source=temporal_observation.source,
                    sensitivity=AssociationValueSensitivity.NON_SENSITIVE,
                )
            )
    if provenance.source_system:
        source = "source_provenance.entity.source_system"
        value = _normalize_text(provenance.source_system)
        values.append(
            AssociationReadyValue(
                name=AssociationDimension.SAME_SOURCE_SYSTEM.value,
                value=value,
                observation_id=_synthetic_observation_id(
                    document_id, source, "source_system", value
                ),
                field="source_system",
                source=source,
                sensitivity=AssociationValueSensitivity.INTERNAL,
            )
        )
    for relation in provenance.asserted_relations:
        if relation.role not in _PARENT_COLLECTION_ROLES:
            continue
        source = f"source_provenance.relations.{relation.role}"
        values.append(
            AssociationReadyValue(
                name=AssociationDimension.SAME_PARENT_COLLECTION.value,
                value=relation.target_id,
                observation_id=_synthetic_observation_id(
                    document_id, source, relation.target_type, relation.target_id
                ),
                field=relation.role,
                source=source,
                sensitivity=AssociationValueSensitivity.INTERNAL,
            )
        )
    unique = {
        (item.name, item.value, item.observation_id, item.source): item for item in values
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            _DIMENSION_PRIORITY[AssociationDimension(item.name)],
            item.value,
            item.observation_id,
            item.source,
        ),
    )


def _conflict(
    *,
    code: InvestigationConflictCode,
    status: InvestigationStatus,
    document_id: str,
    observations: Sequence[DocumentTemporalObservation] = (),
    sources: Sequence[str] = (),
    detail: str,
) -> InvestigationMetadataConflict:
    return InvestigationMetadataConflict(
        code=code,
        status=status,
        document_id=document_id,
        observation_ids=sorted({item.observation_id for item in observations}),
        sources=sorted({*sources, *(item.source for item in observations)}),
        detail=detail,
    )


def _investigation_conflicts(
    profile: DocumentMetadataProfile,
    temporal: Sequence[DocumentTemporalObservation],
) -> list[InvestigationMetadataConflict]:
    document_id = profile.entity_id
    conflicts: list[InvestigationMetadataConflict] = []
    for item in temporal:
        if item.timezone_status == InvestigationTimezoneStatus.UNKNOWN:
            conflicts.append(
                _conflict(
                    code=InvestigationConflictCode.TIMEZONE_UNKNOWN,
                    status=InvestigationStatus.UNKNOWN,
                    document_id=document_id,
                    observations=[item],
                    detail="Instant ordering and UTC calendar periods are indeterminate.",
                )
            )
        elif item.timezone_status == InvestigationTimezoneStatus.INVALID:
            conflicts.append(
                _conflict(
                    code=InvestigationConflictCode.INVALID_TIMESTAMP,
                    status=InvestigationStatus.INVALID,
                    document_id=document_id,
                    observations=[item],
                    detail="Raw timestamp is preserved but cannot be normalized.",
                )
            )
        parsed = _parse_datetime(item.instant_utc)
        extracted_at = item.extracted_at
        if parsed and extracted_at.tzinfo and parsed > extracted_at.astimezone(UTC):
            conflicts.append(
                _conflict(
                    code=InvestigationConflictCode.FUTURE_TIMESTAMP,
                    status=InvestigationStatus.CONFLICTING,
                    document_id=document_id,
                    observations=[item],
                    detail="Observed timestamp is later than its extraction time.",
                )
            )

    temporal_by_role: dict[TemporalSemanticRole, list[DocumentTemporalObservation]] = defaultdict(
        list
    )
    for item in temporal:
        temporal_by_role[item.semantic_role].append(item)
    for role, code in (
        (
            TemporalSemanticRole.PRODUCTION,
            InvestigationConflictCode.MULTIPLE_CREATION_OBSERVATIONS,
        ),
        (
            TemporalSemanticRole.MODIFICATION,
            InvestigationConflictCode.MULTIPLE_MODIFICATION_OBSERVATIONS,
        ),
    ):
        observations = temporal_by_role[role]
        values = {item.normalized_value or str(item.raw_value) for item in observations}
        if len(values) > 1:
            conflicts.append(
                _conflict(
                    code=code,
                    status=InvestigationStatus.CONFLICTING,
                    document_id=document_id,
                    observations=observations,
                    detail="Multiple source-qualified observations are preserved without resolution.",
                )
            )

    observations_by_field: dict[str, list[MetadataObservation]] = defaultdict(list)
    for metadata_observation in profile.observations():
        observations_by_field[metadata_observation.field].append(metadata_observation)
    for profile_conflict in profile.conflicts:
        related = observations_by_field.get(profile_conflict.field, [])
        conflicts.append(
            InvestigationMetadataConflict(
                code=InvestigationConflictCode.SOURCE_CONFLICT,
                status=InvestigationStatus.CONFLICTING,
                document_id=document_id,
                observation_ids=sorted(
                    _observation_id(document_id, observation) for observation in related
                ),
                sources=sorted(profile_conflict.sources),
                detail=(
                    f"Conflicting {profile_conflict.field} observations remain unresolved."
                ),
            )
        )

    creators = [
        item
        for item in profile.observations()
        if item.field in _CREATOR_FIELDS and item.value is not None
    ]
    modifiers = [
        item
        for item in profile.observations()
        if item.field == "last_modified_by" and item.value is not None
    ]
    creator_values = {
        _normalize_text(value)
        for item in creators
        for value in _values(item.value)
        if value is not None
    }
    modifier_values = {
        _normalize_text(value)
        for item in modifiers
        for value in _values(item.value)
        if value is not None
    }
    if creator_values and modifier_values and creator_values.isdisjoint(modifier_values):
        combined = [*creators, *modifiers]
        conflicts.append(
            InvestigationMetadataConflict(
                code=InvestigationConflictCode.CREATOR_CHANGED,
                status=InvestigationStatus.OBSERVED,
                document_id=document_id,
                observation_ids=sorted(
                    _observation_id(document_id, observation) for observation in combined
                ),
                sources=sorted({observation.source for observation in combined}),
                detail="Observed creator and last modifier differ; no person identity is inferred.",
            )
        )

    production = temporal_by_role[TemporalSemanticRole.PRODUCTION]
    modification = temporal_by_role[TemporalSemanticRole.MODIFICATION]
    inverted: list[tuple[DocumentTemporalObservation, DocumentTemporalObservation]] = []
    for created, modified in product(production, modification):
        created_instant = _parse_datetime(created.instant_utc)
        modified_instant = _parse_datetime(modified.instant_utc)
        if created_instant is not None and modified_instant is not None:
            if modified_instant < created_instant:
                inverted.append((created, modified))
    if inverted:
        conflicts.append(
            _conflict(
                code=InvestigationConflictCode.MODIFIED_BEFORE_CREATED,
                status=InvestigationStatus.CONFLICTING,
                document_id=document_id,
                observations=[item for pair in inverted for item in pair],
                detail="At least one comparable modification precedes a production observation.",
            )
        )

    archive = [
        item
        for role in (
            TemporalSemanticRole.ARCHIVED,
            TemporalSemanticRole.ARCHIVE_CREATION,
            TemporalSemanticRole.ARCHIVE_MODIFICATION,
        )
        for item in temporal_by_role[role]
    ]
    archive_inverted: list[
        tuple[DocumentTemporalObservation, DocumentTemporalObservation]
    ] = []
    for created, archived in product(production, archive):
        created_instant = _parse_datetime(created.instant_utc)
        archived_instant = _parse_datetime(archived.instant_utc)
        if created_instant is not None and archived_instant is not None:
            if archived_instant < created_instant:
                archive_inverted.append((created, archived))
    if archive_inverted:
        conflicts.append(
            _conflict(
                code=InvestigationConflictCode.ARCHIVE_EMBEDDED_DATE_INVERSION,
                status=InvestigationStatus.CONFLICTING,
                document_id=document_id,
                observations=[item for pair in archive_inverted for item in pair],
                detail="An archive timestamp precedes an embedded production observation.",
            )
        )

    unique = {
        _canonical_json(item.model_dump(mode="json")): item for item in conflicts
    }
    return sorted(
        unique.values(),
        key=lambda item: (item.code.value, item.observation_ids, item.sources, item.detail),
    )


def inspect_document_metadata(
    profile: DocumentMetadataProfile,
    *,
    source_provenance: SourceProvenance | None = None,
) -> DocumentMetadataInspection:
    """Normalize one v1 profile into internal, association-ready evidence."""
    document_id = profile.entity_id
    observations = sorted(
        profile.observations(),
        key=lambda item: _canonical_json(item.canonical_fact()),
    )
    temporal = sorted(
        (
            temporal_item
            for observation in observations
            if (temporal_item := _temporal_observation(document_id, observation)) is not None
        ),
        key=lambda item: (item.semantic_role.value, item.observation_id),
    )
    provenance = _safe_provenance(document_id, source_provenance)
    ready_values = _association_ready_values(document_id, observations, temporal, provenance)
    keys_by_identity: dict[tuple[AssociationDimension, str], AssociationCandidateKey] = {}
    for ready_value in ready_values:
        dimension = AssociationDimension(ready_value.name)
        key = AssociationCandidateKey(
            dimension=dimension,
            value_sha256=_sha256(
                {"dimension": dimension.value, "value": ready_value.value}
            ),
        )
        keys_by_identity[(dimension, key.value_sha256)] = key
    keys = sorted(
        keys_by_identity.values(),
        key=lambda item: (_DIMENSION_PRIORITY[item.dimension], item.value_sha256),
    )
    return DocumentMetadataInspection(
        document_id=document_id,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        observations=observations,
        temporal_observations=temporal,
        conflicts=_investigation_conflicts(profile, temporal),
        safe_provenance=provenance,
        association_ready_values=ready_values,
        association_keys=keys,
    )


def _ready_by_dimension(
    inspection: DocumentMetadataInspection,
) -> dict[AssociationDimension, list[AssociationReadyValue]]:
    result: dict[AssociationDimension, list[AssociationReadyValue]] = defaultdict(list)
    for item in inspection.association_ready_values:
        result[AssociationDimension(item.name)].append(item)
    return result


def _evidence(
    dimension: AssociationDimension,
    left: AssociationReadyValue,
    right: AssociationReadyValue,
) -> AssociationEvidence:
    evidence_status = (
        InvestigationStatus.ASSERTED
        if dimension == AssociationDimension.SAME_PARENT_COLLECTION
        else InvestigationStatus.OBSERVED
    )
    basis = _CALENDAR_DIMENSION_BASIS.get(dimension)
    is_calendar = basis is not None
    material = {
        "dimension": dimension.value,
        "left_observation_id": left.observation_id,
        "right_observation_id": right.observation_id,
        "left_source": left.source,
        "right_source": right.source,
        "value": left.value,
        "calendar_basis": basis.value if basis else None,
    }
    return AssociationEvidence(
        evidence_id=_sha256(material),
        dimension=dimension,
        status=evidence_status,
        left_observation_id=left.observation_id,
        right_observation_id=right.observation_id,
        left_field=left.field,
        right_field=right.field,
        left_source=left.source,
        right_source=right.source,
        comparison_value=left.value,
        calendar_basis=basis,
        normalized_period=left.value if is_calendar else None,
    )


def compare_document_metadata(
    left: DocumentMetadataInspection,
    right: DocumentMetadataInspection,
    *,
    dimensions: Iterable[AssociationDimension] | None = None,
) -> DocumentMetadataComparison:
    """Compare two occurrences without inferring lineage or preferred truth."""
    if left.document_id == right.document_id:
        raise ValueError("document metadata comparison requires distinct occurrence identities")
    if left.document_id > right.document_id:
        left, right = right, left
    selected = set(dimensions) if dimensions is not None else set(AssociationDimension)

    temporal_relations: list[TemporalRelation] = []
    calendar_associations: list[CalendarPeriodAssociation] = []
    for left_time, right_time in product(
        left.temporal_observations,
        right.temporal_observations,
    ):
        if left_time.semantic_role != right_time.semantic_role:
            continue
        temporal_relations.append(compare_temporal_observations(left_time, right_time))
        for basis, granularity in product(CalendarBasis, CalendarGranularity):
            calendar_associations.append(
                compare_calendar_periods(
                    left_time,
                    right_time,
                    basis=basis,
                    granularity=granularity,
                )
            )

    left_values = _ready_by_dimension(left)
    right_values = _ready_by_dimension(right)
    dimension_results: list[AssociationDimensionResult] = []
    all_evidence: list[AssociationEvidence] = []
    for dimension in ASSOCIATION_DIMENSION_PRIORITY:
        if dimension not in selected:
            continue
        left_items = left_values.get(dimension, [])
        right_items = right_values.get(dimension, [])
        left_set = {item.value for item in left_items}
        right_set = {item.value for item in right_items}
        overlap = left_set & right_set
        if not overlap:
            continue
        evidence = sorted(
            (
                _evidence(dimension, left_item, right_item)
                for value in sorted(overlap)
                for left_item in left_items
                if left_item.value == value
                for right_item in right_items
                if right_item.value == value
            ),
            key=lambda item: (
                item.left_observation_id,
                item.right_observation_id,
                item.left_source,
                item.right_source,
                item.evidence_id,
            ),
        )
        evidence_by_id = {item.evidence_id: item for item in evidence}
        evidence = [evidence_by_id[key] for key in sorted(evidence_by_id)]
        status = (
            InvestigationStatus.ASSOCIATED
            if left_set == right_set
            else InvestigationStatus.CONFLICTING
        )
        dimension_results.append(
            AssociationDimensionResult(
                dimension=dimension,
                status=status,
                evidence_ids=[item.evidence_id for item in evidence],
            )
        )
        all_evidence.extend(evidence)

    all_evidence.sort(
        key=lambda item: (
            _DIMENSION_PRIORITY[item.dimension],
            item.evidence_id,
        )
    )
    dimensions_found = [item.dimension for item in dimension_results]
    temporal_relations.sort(
        key=lambda item: (item.left_observation_id, item.right_observation_id)
    )
    calendar_associations.sort(
        key=lambda item: (
            item.left_observation_id,
            item.right_observation_id,
            item.basis.value,
            item.granularity.value,
        )
    )
    return DocumentMetadataComparison(
        left_document_id=left.document_id,
        right_document_id=right.document_id,
        temporal_relations=temporal_relations,
        calendar_period_associations=calendar_associations,
        dimensions=dimensions_found,
        dimension_results=dimension_results,
        evidence=all_evidence,
    )


_DIMENSION_FAMILIES: dict[AssociationDimension, str] = {
    **{
        dimension: "temporal"
        for dimension in (
            *tuple(_LOCAL_TEMPORAL_DIMENSIONS.values()),
            *tuple(_UTC_TEMPORAL_DIMENSIONS.values()),
            *tuple(_INSTANT_DIMENSIONS.values()),
        )
    },
    AssociationDimension.SAME_SOURCE_SYSTEM: "source",
    AssociationDimension.SAME_SOURCE_ENTITY_FAMILY: "source",
    AssociationDimension.SAME_PARENT_COLLECTION: "source",
    AssociationDimension.SAME_DOCUMENT_TYPE: "type",
    AssociationDimension.COMPATIBLE_DOCUMENT_TYPES: "type",
    AssociationDimension.SAME_MIME_TYPE: "type",
    AssociationDimension.SAME_EXTENSION: "type",
    AssociationDimension.SAME_CREATOR_OBSERVATION: "actor",
    AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION: "actor",
    AssociationDimension.SAME_PRODUCER_OBSERVATION: "actor",
    AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION: "actor",
    AssociationDimension.SAME_FILENAME_BASENAME: "filename",
    AssociationDimension.SAME_BINARY_HASH: "binary_identity",
}
_MEGA_HUB_ONLY_DIMENSIONS = {
    AssociationDimension.SAME_PRODUCTION_YEAR,
    AssociationDimension.SAME_PRODUCTION_YEAR_UTC,
    AssociationDimension.SAME_MODIFICATION_YEAR,
    AssociationDimension.SAME_MODIFICATION_YEAR_UTC,
    AssociationDimension.SAME_SOURCE_SYSTEM,
    AssociationDimension.SAME_MIME_TYPE,
    AssociationDimension.SAME_EXTENSION,
    AssociationDimension.COMPATIBLE_DOCUMENT_TYPES,
}
_DISCRIMINATING_DIMENSIONS = {
    AssociationDimension.SAME_SOURCE_ENTITY_FAMILY,
    AssociationDimension.SAME_PRODUCTION_INSTANT,
    AssociationDimension.SAME_MODIFICATION_INSTANT,
    AssociationDimension.SAME_PRODUCTION_DAY,
    AssociationDimension.SAME_PRODUCTION_DAY_UTC,
    AssociationDimension.SAME_MODIFICATION_DAY,
    AssociationDimension.SAME_MODIFICATION_DAY_UTC,
    AssociationDimension.SAME_CREATOR_OBSERVATION,
    AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION,
    AssociationDimension.SAME_PRODUCER_OBSERVATION,
    AssociationDimension.SAME_DOCUMENT_TYPE,
    AssociationDimension.SAME_FILENAME_BASENAME,
}


def classify_association_strength(
    dimensions: Iterable[AssociationDimension],
) -> AssociationStrength:
    """Apply the deterministic, score-free combination policy v1."""
    dimension_set = set(dimensions)
    if not dimension_set:
        return AssociationStrength.NONE
    if dimension_set & {
        AssociationDimension.SAME_BINARY_HASH,
        AssociationDimension.SAME_PARENT_COLLECTION,
    }:
        return AssociationStrength.STRONG
    families = {_DIMENSION_FAMILIES[dimension] for dimension in dimension_set}
    if (
        AssociationDimension.SAME_SOURCE_ENTITY_FAMILY in dimension_set
        and len(families) >= 2
    ):
        return AssociationStrength.MEDIUM
    if len(families) >= 3 and dimension_set & _DISCRIMINATING_DIMENSIONS:
        return AssociationStrength.MEDIUM
    if dimension_set <= _MEGA_HUB_ONLY_DIMENSIONS:
        return AssociationStrength.VERY_WEAK
    return AssociationStrength.WEAK


def build_document_association(
    left: DocumentMetadataInspection,
    right: DocumentMetadataInspection,
    *,
    dimensions: Iterable[AssociationDimension] | None = None,
) -> DocumentAssociation:
    comparison = compare_document_metadata(left, right, dimensions=dimensions)
    strength = classify_association_strength(comparison.dimensions)
    if not comparison.dimensions:
        status = InvestigationStatus.UNKNOWN
    elif any(
        item.status == InvestigationStatus.CONFLICTING
        for item in comparison.dimension_results
    ):
        status = InvestigationStatus.CONFLICTING
    else:
        status = InvestigationStatus.ASSOCIATED
    return DocumentAssociation(
        left_document_id=comparison.left_document_id,
        right_document_id=comparison.right_document_id,
        dimensions=comparison.dimensions,
        dimension_results=comparison.dimension_results,
        evidence=comparison.evidence,
        association_strength=strength,
        association_status=status,
    )


def build_document_chronology(
    profile_or_inspection: DocumentMetadataProfile | DocumentMetadataInspection,
    *,
    source_provenance: SourceProvenance | None = None,
) -> DocumentChronology:
    """Build one occurrence chronology while retaining partial order."""
    inspection = (
        profile_or_inspection
        if isinstance(profile_or_inspection, DocumentMetadataInspection)
        else inspect_document_metadata(
            profile_or_inspection,
            source_provenance=source_provenance,
        )
    )
    comparable: list[TemporalRelation] = []
    indeterminate: list[TemporalRelation] = []
    calendar: list[CalendarPeriodAssociation] = []
    for left, right in combinations(inspection.temporal_observations, 2):
        relation = compare_temporal_observations(left, right)
        target = (
            indeterminate
            if relation.relation == TemporalRelationKind.INDETERMINATE
            else comparable
        )
        target.append(relation)
        if left.semantic_role == right.semantic_role:
            for basis, granularity in product(CalendarBasis, CalendarGranularity):
                calendar.append(
                    compare_calendar_periods(
                        left,
                        right,
                        basis=basis,
                        granularity=granularity,
                    )
                )
    def relation_key(item: TemporalRelation) -> tuple[str, str]:
        return item.left_observation_id, item.right_observation_id

    comparable.sort(key=relation_key)
    indeterminate.sort(key=relation_key)
    calendar.sort(
        key=lambda item: (
            item.left_observation_id,
            item.right_observation_id,
            item.basis.value,
            item.granularity.value,
        )
    )
    return DocumentChronology(
        document_id=inspection.document_id,
        temporal_observations=inspection.temporal_observations,
        comparable_relations=comparable,
        calendar_period_associations=calendar,
        conflicts=inspection.conflicts,
        indeterminate_relations=indeterminate,
        provenance=inspection.safe_provenance,
    )


def build_candidate_lineage_evidence(
    association: DocumentAssociation,
) -> CandidateLineageEvidence:
    """Project candidate-only evidence without emitting a lineage edge."""
    candidate_dimensions = {
        AssociationDimension.SAME_SOURCE_SYSTEM,
        AssociationDimension.SAME_SOURCE_ENTITY_FAMILY,
        AssociationDimension.SAME_PARENT_COLLECTION,
        AssociationDimension.SAME_PRODUCTION_INSTANT,
        AssociationDimension.SAME_PRODUCTION_DAY,
        AssociationDimension.SAME_PRODUCTION_MONTH,
        AssociationDimension.SAME_PRODUCTION_YEAR,
        AssociationDimension.SAME_CREATOR_OBSERVATION,
        AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION,
        AssociationDimension.SAME_PRODUCER_OBSERVATION,
        AssociationDimension.SAME_DOCUMENT_TYPE,
        AssociationDimension.COMPATIBLE_DOCUMENT_TYPES,
        AssociationDimension.SAME_MIME_TYPE,
        AssociationDimension.SAME_FILENAME_BASENAME,
        AssociationDimension.SAME_EXTENSION,
        AssociationDimension.SAME_BINARY_HASH,
    }
    dimensions = [
        dimension for dimension in association.dimensions if dimension in candidate_dimensions
    ]
    evidence = [
        item for item in association.evidence if item.dimension in candidate_dimensions
    ]
    return CandidateLineageEvidence(
        left_document_id=association.left_document_id,
        right_document_id=association.right_document_id,
        dimensions=dimensions,
        evidence=evidence,
    )


def project_document_metadata_evidence(
    inspection: DocumentMetadataInspection,
    *,
    accessible_document_ids: set[str],
    max_temporal_observations: int = 12,
    max_conflicts: int = 8,
) -> DocumentMetadataEvidenceProjection | None:
    """Return a bounded safe projection, or nothing across a DLS boundary."""
    if inspection.document_id not in accessible_document_ids:
        return None
    temporal = inspection.temporal_observations[:max_temporal_observations]
    conflicts = inspection.conflicts[:max_conflicts]
    return DocumentMetadataEvidenceProjection(
        document_id=inspection.document_id,
        status=(
            InvestigationStatus.CONFLICTING if conflicts else InvestigationStatus.OBSERVED
        ),
        temporal_observations=[
            {
                "semantic_role": item.semantic_role.value,
                "normalized_value": item.normalized_value,
                "timezone_status": item.timezone_status.value,
                "source": item.source,
            }
            for item in temporal
        ],
        conflicts=[
            {"code": item.code.value, "status": item.status.value, "detail": item.detail}
            for item in conflicts
        ],
        truncated=(
            len(inspection.temporal_observations) > len(temporal)
            or len(inspection.conflicts) > len(conflicts)
        ),
    )


_DIMENSION_REASON = {
    AssociationDimension.SAME_BINARY_HASH: (
        "same observed binary SHA-256; source occurrences remain distinct"
    ),
    AssociationDimension.SAME_PARENT_COLLECTION: (
        "same explicitly asserted parent collection"
    ),
    AssociationDimension.SAME_SOURCE_SYSTEM: "same observed source system",
    AssociationDimension.SAME_SOURCE_ENTITY_FAMILY: "same observed source entity family",
    AssociationDimension.SAME_DOCUMENT_TYPE: "same explicitly source-declared document type",
    AssociationDimension.COMPATIBLE_DOCUMENT_TYPES: "compatible technical format families",
    AssociationDimension.SAME_CREATOR_OBSERVATION: "same exact observed creator value",
    AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION: (
        "same exact observed last-modifier value"
    ),
    AssociationDimension.SAME_PRODUCER_OBSERVATION: "same exact observed producer value",
    AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION: (
        "same exact observed creator-application value"
    ),
    AssociationDimension.SAME_MIME_TYPE: "same technical MIME type",
    AssociationDimension.SAME_FILENAME_BASENAME: "same filename basename (weak evidence)",
    AssociationDimension.SAME_EXTENSION: "same file extension",
    AssociationDimension.SAME_PRODUCTION_INSTANT: "same comparable production instant",
    AssociationDimension.SAME_MODIFICATION_INSTANT: "same comparable modification instant",
}


def _dimension_reason(
    dimension: AssociationDimension,
    evidence: Sequence[AssociationEvidence],
) -> str:
    if dimension in _DIMENSION_REASON:
        return _DIMENSION_REASON[dimension]
    item = next((value for value in evidence if value.dimension == dimension), None)
    period = f": {item.normalized_period}" if item and item.normalized_period else ""
    basis = _CALENDAR_DIMENSION_BASIS.get(dimension)
    basis_label = "source-local calendar" if basis == CalendarBasis.SOURCE_LOCAL else "UTC calendar"
    role = "production" if "PRODUCTION" in dimension.value else "modification"
    granularity = next(
        value for value in ("day", "month", "year") if value.upper() in dimension.value
    )
    return f"same observed {role} {granularity} ({basis_label}){period}"


def project_association_evidence(
    association: DocumentAssociation,
    *,
    accessible_document_ids: set[str],
    max_dimensions: int = 8,
) -> DocumentAssociationEvidenceProjection | None:
    """DLS-safe, bounded, stable explanation without raw sensitive values."""
    if not {
        association.left_document_id,
        association.right_document_id,
    } <= accessible_document_ids:
        return None
    dimensions = association.dimensions[:max_dimensions]
    reasons = [
        _dimension_reason(dimension, association.evidence) for dimension in dimensions
    ]
    return DocumentAssociationEvidenceProjection(
        left_document_id=association.left_document_id,
        right_document_id=association.right_document_id,
        association_strength=association.association_strength,
        association_status=association.association_status,
        reasons=reasons,
        truncated=len(association.dimensions) > len(dimensions),
    )


_STRENGTH_PRIORITY = {
    AssociationStrength.STRONG: 0,
    AssociationStrength.MEDIUM: 1,
    AssociationStrength.WEAK: 2,
    AssociationStrength.VERY_WEAK: 3,
    AssociationStrength.NONE: 4,
}


def _association_order(association: DocumentAssociation) -> tuple[Any, ...]:
    best_dimension = min(
        (_DIMENSION_PRIORITY[item] for item in association.dimensions),
        default=len(_DIMENSION_PRIORITY),
    )
    return (
        _STRENGTH_PRIORITY[association.association_strength],
        best_dimension,
        association.left_document_id,
        association.right_document_id,
        association.document_association_sha256,
    )


def _within_time_window(
    left: DocumentMetadataInspection,
    right: DocumentMetadataInspection,
    days: int,
) -> bool:
    left_times = [
        _parse_datetime(item.instant_utc)
        for item in left.temporal_observations
        if item.semantic_role == TemporalSemanticRole.PRODUCTION and item.instant_utc
    ]
    right_times = [
        _parse_datetime(item.instant_utc)
        for item in right.temporal_observations
        if item.semantic_role == TemporalSemanticRole.PRODUCTION and item.instant_utc
    ]
    comparable = [
        abs((left_time - right_time).total_seconds())
        for left_time, right_time in product(left_times, right_times)
        if left_time is not None and right_time is not None
    ]
    return bool(comparable) and min(comparable) <= days * 86_400


def _source_systems(inspection: DocumentMetadataInspection) -> set[str]:
    return {
        item.value
        for item in inspection.association_ready_values
        if item.name == AssociationDimension.SAME_SOURCE_SYSTEM.value
    }


def build_documentary_neighborhood(
    seed_document_ids: Sequence[str],
    inspections: Sequence[DocumentMetadataInspection],
    *,
    accessible_document_ids: set[str],
    dimensions: Iterable[AssociationDimension] | None = None,
    limits: NeighborhoodLimits | None = None,
) -> DocumentaryNeighborhood:
    """Build a DLS-filtered, key-bucketed, seed-centric bounded neighborhood."""
    limits = limits or NeighborhoodLimits()
    seeds = sorted(set(seed_document_ids))
    if not seeds:
        raise ValueError("at least one seed document is required")
    if len(seeds) > limits.max_documents:
        raise ValueError("max_documents must be at least the number of seeds")
    selected = set(dimensions) if dimensions is not None else set(AssociationDimension)
    by_id: dict[str, DocumentMetadataInspection] = {}
    for inspection in inspections:
        if inspection.document_id in by_id:
            raise ValueError("documentary neighborhood requires unique occurrence identities")
        if inspection.document_id not in accessible_document_ids:
            continue
        if limits.source_scope and not (
            _source_systems(inspection) & {_normalize_text(item) for item in limits.source_scope}
        ):
            continue
        by_id[inspection.document_id] = inspection
    if any(seed not in by_id for seed in seeds):
        raise ValueError("one or more seed documents are unavailable in the accessible scope")

    buckets: dict[tuple[AssociationDimension, str], list[str]] = defaultdict(list)
    for document_id, inspection in by_id.items():
        for key in inspection.association_keys:
            if key.dimension in selected:
                buckets[(key.dimension, key.value_sha256)].append(document_id)
    for bucket in buckets.values():
        bucket.sort()

    candidate_pairs: set[tuple[str, str]] = set()
    truncated_dimensions: set[AssociationDimension] = set()
    for seed in seeds:
        inspection = by_id[seed]
        keys_by_dimension: dict[AssociationDimension, list[AssociationCandidateKey]] = (
            defaultdict(list)
        )
        for key in inspection.association_keys:
            if key.dimension in selected:
                keys_by_dimension[key.dimension].append(key)
        for dimension in sorted(
            keys_by_dimension,
            key=lambda item: _DIMENSION_PRIORITY[item],
        ):
            candidates = sorted(
                {
                    candidate
                    for key in keys_by_dimension[dimension]
                    for candidate in buckets[(dimension, key.value_sha256)]
                    if candidate != seed
                }
            )
            if len(candidates) > limits.per_dimension_limit:
                truncated_dimensions.add(dimension)
            for candidate in candidates[: limits.per_dimension_limit]:
                pair = (seed, candidate) if seed < candidate else (candidate, seed)
                candidate_pairs.add(pair)

    associations: list[DocumentAssociation] = []
    for left_id, right_id in sorted(candidate_pairs):
        left = by_id[left_id]
        right = by_id[right_id]
        if limits.time_window_days is not None and not _within_time_window(
            left,
            right,
            limits.time_window_days,
        ):
            continue
        association = build_document_association(left, right, dimensions=selected)
        if association.dimensions:
            associations.append(association)
    associations.sort(key=_association_order)
    if len(associations) > limits.max_associations:
        for association in associations[limits.max_associations :]:
            truncated_dimensions.update(association.dimensions)
        associations = associations[: limits.max_associations]

    included = set(seeds)
    retained: list[DocumentAssociation] = []
    for association in associations:
        additions = {
            association.left_document_id,
            association.right_document_id,
        } - included
        if len(included) + len(additions) > limits.max_documents:
            truncated_dimensions.update(association.dimensions)
            continue
        included.update(additions)
        retained.append(association)

    inclusion_data: dict[str, tuple[set[str], set[AssociationDimension]]] = {}
    for association in retained:
        for document_id in (association.left_document_id, association.right_document_id):
            if document_id in seeds:
                continue
            ids, reasons = inclusion_data.setdefault(document_id, (set(), set()))
            ids.add(association.document_association_sha256)
            reasons.update(association.dimensions)
    inclusions = [
        NeighborhoodInclusion(
            document_id=document_id,
            association_ids=sorted(ids),
            dimensions=sorted(reasons, key=lambda item: _DIMENSION_PRIORITY[item]),
        )
        for document_id, (ids, reasons) in sorted(inclusion_data.items())
    ]
    ranked_documents = [*seeds, *(item.document_id for item in inclusions)]
    return DocumentaryNeighborhood(
        seed_document_ids=seeds,
        document_ids=list(dict.fromkeys(ranked_documents)),
        associations=retained,
        inclusions=inclusions,
        limits=limits,
        truncated_dimensions=sorted(
            truncated_dimensions,
            key=lambda item: _DIMENSION_PRIORITY[item],
        ),
    )
