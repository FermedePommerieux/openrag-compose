"""Offline, auditable document-investigation semantics.

These models consume ``openrag.document-metadata v1`` observations without
changing that profile.  Associations are descriptive evidence: they are not
PROV-O relations, do not establish truth, and are always non-scope-expanding.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.document_metadata import (
    MetadataObservation,
    MetadataSourceType,
    MetadataTrustClass,
)

DOCUMENT_INVESTIGATION_POLICY_ID = "openrag.document-investigation-association"
DOCUMENT_INVESTIGATION_POLICY_VERSION = 1


class InvestigationStatus(StrEnum):
    """Evidence states; none of them is an unqualified truth assertion."""

    ASSERTED = "ASSERTED"
    OBSERVED = "OBSERVED"
    ASSOCIATED = "ASSOCIATED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"
    CONFLICTING = "CONFLICTING"
    INVALID = "INVALID"


class TemporalSemanticRole(StrEnum):
    PRODUCTION = "PRODUCTION"
    MODIFICATION = "MODIFICATION"
    DIGITIZATION = "DIGITIZATION"
    FILESYSTEM_BIRTHTIME = "FILESYSTEM_BIRTHTIME"
    FILESYSTEM_MODIFICATION = "FILESYSTEM_MODIFICATION"
    FILESYSTEM_CHANGE = "FILESYSTEM_CHANGE"
    ARCHIVED = "ARCHIVED"
    ARCHIVE_CREATION = "ARCHIVE_CREATION"
    ARCHIVE_MODIFICATION = "ARCHIVE_MODIFICATION"
    INGESTION = "INGESTION"


class InvestigationTimezoneStatus(StrEnum):
    EXPLICIT_OFFSET = "EXPLICIT_OFFSET"
    ASSUMED_BY_FORMAT = "ASSUMED_BY_FORMAT"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


class TemporalRelationKind(StrEnum):
    BEFORE = "BEFORE"
    AFTER = "AFTER"
    EQUAL = "EQUAL"
    INDETERMINATE = "INDETERMINATE"


class CalendarBasis(StrEnum):
    SOURCE_LOCAL = "SOURCE_LOCAL"
    UTC = "UTC"


class CalendarGranularity(StrEnum):
    DAY = "DAY"
    MONTH = "MONTH"
    YEAR = "YEAR"


class CalendarMatchStatus(StrEnum):
    MATCH = "MATCH"
    DIFFERENT = "DIFFERENT"
    INDETERMINATE = "INDETERMINATE"


class InvestigationConflictCode(StrEnum):
    TIMEZONE_UNKNOWN = "TIMEZONE_UNKNOWN"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    MULTIPLE_CREATION_OBSERVATIONS = "MULTIPLE_CREATION_OBSERVATIONS"
    MULTIPLE_MODIFICATION_OBSERVATIONS = "MULTIPLE_MODIFICATION_OBSERVATIONS"
    CREATOR_CHANGED = "CREATOR_CHANGED"
    MODIFIED_BEFORE_CREATED = "MODIFIED_BEFORE_CREATED"
    ARCHIVE_EMBEDDED_DATE_INVERSION = "ARCHIVE_EMBEDDED_DATE_INVERSION"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"


class AssociationDimension(StrEnum):
    SAME_PRODUCTION_INSTANT = "SAME_PRODUCTION_INSTANT"
    SAME_PRODUCTION_DAY = "SAME_PRODUCTION_DAY"
    SAME_PRODUCTION_MONTH = "SAME_PRODUCTION_MONTH"
    SAME_PRODUCTION_YEAR = "SAME_PRODUCTION_YEAR"
    SAME_PRODUCTION_DAY_UTC = "SAME_PRODUCTION_DAY_UTC"
    SAME_PRODUCTION_MONTH_UTC = "SAME_PRODUCTION_MONTH_UTC"
    SAME_PRODUCTION_YEAR_UTC = "SAME_PRODUCTION_YEAR_UTC"
    SAME_MODIFICATION_INSTANT = "SAME_MODIFICATION_INSTANT"
    SAME_MODIFICATION_DAY = "SAME_MODIFICATION_DAY"
    SAME_MODIFICATION_MONTH = "SAME_MODIFICATION_MONTH"
    SAME_MODIFICATION_YEAR = "SAME_MODIFICATION_YEAR"
    SAME_MODIFICATION_DAY_UTC = "SAME_MODIFICATION_DAY_UTC"
    SAME_MODIFICATION_MONTH_UTC = "SAME_MODIFICATION_MONTH_UTC"
    SAME_MODIFICATION_YEAR_UTC = "SAME_MODIFICATION_YEAR_UTC"
    SAME_SOURCE_SYSTEM = "SAME_SOURCE_SYSTEM"
    SAME_SOURCE_ENTITY_FAMILY = "SAME_SOURCE_ENTITY_FAMILY"
    SAME_PARENT_COLLECTION = "SAME_PARENT_COLLECTION"
    SAME_DOCUMENT_TYPE = "SAME_DOCUMENT_TYPE"
    COMPATIBLE_DOCUMENT_TYPES = "COMPATIBLE_DOCUMENT_TYPES"
    SAME_CREATOR_OBSERVATION = "SAME_CREATOR_OBSERVATION"
    SAME_LAST_MODIFIER_OBSERVATION = "SAME_LAST_MODIFIER_OBSERVATION"
    SAME_PRODUCER_OBSERVATION = "SAME_PRODUCER_OBSERVATION"
    SAME_CREATOR_APPLICATION_OBSERVATION = "SAME_CREATOR_APPLICATION_OBSERVATION"
    SAME_MIME_TYPE = "SAME_MIME_TYPE"
    SAME_FILENAME_BASENAME = "SAME_FILENAME_BASENAME"
    SAME_EXTENSION = "SAME_EXTENSION"
    SAME_BINARY_HASH = "SAME_BINARY_HASH"


class AssociationStrength(StrEnum):
    STRONG = "STRONG"
    MEDIUM = "MEDIUM"
    WEAK = "WEAK"
    VERY_WEAK = "VERY_WEAK"
    NONE = "NONE"


class AssociationValueSensitivity(StrEnum):
    NON_SENSITIVE = "NON_SENSITIVE"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"


class NeighborhoodCompleteness(StrEnum):
    BOUNDED_NOT_EXHAUSTIVE = "BOUNDED_NOT_EXHAUSTIVE"


class DocumentTemporalObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str
    document_id: str
    semantic_role: TemporalSemanticRole
    field: str
    raw_value: str | int | float | bool | list[str] | None = None
    normalized_value: str | None = None
    timezone_status: InvestigationTimezoneStatus
    timezone: str | None = None
    source: str
    source_type: MetadataSourceType
    trust_class: MetadataTrustClass
    status: InvestigationStatus
    extracted_at: datetime
    normalization_version: int
    instant_utc: str | None = None
    source_local_day: str | None = None
    source_local_month: str | None = None
    source_local_year: str | None = None
    utc_day: str | None = None
    utc_month: str | None = None
    utc_year: str | None = None

    @model_validator(mode="after")
    def validate_timezone_state(self) -> DocumentTemporalObservation:
        if self.timezone_status == InvestigationTimezoneStatus.INVALID:
            if self.status != InvestigationStatus.INVALID or self.normalized_value is not None:
                raise ValueError("invalid temporal observations must remain invalid and unnormalized")
        if self.timezone_status == InvestigationTimezoneStatus.UNKNOWN and self.instant_utc:
            raise ValueError("timezone-unknown observations cannot expose a UTC instant")
        if self.timezone_status == InvestigationTimezoneStatus.EXPLICIT_OFFSET:
            if not self.timezone or not self.instant_utc:
                raise ValueError("explicit-offset observations require timezone and UTC instant")
        return self


class TemporalRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_document_id: str
    right_document_id: str
    left_observation_id: str
    right_observation_id: str
    relation: TemporalRelationKind
    comparison_basis: Literal["UTC_INSTANT"] = "UTC_INSTANT"


class CalendarPeriodAssociation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_document_id: str
    right_document_id: str
    left_observation_id: str
    right_observation_id: str
    semantic_role: TemporalSemanticRole
    basis: CalendarBasis
    granularity: CalendarGranularity
    status: CalendarMatchStatus
    left_period: str | None = None
    right_period: str | None = None
    normalized_period: str | None = None


class InvestigationMetadataConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: InvestigationConflictCode
    status: InvestigationStatus
    document_id: str
    observation_ids: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    detail: str


class SafeProvenanceRelation(BaseModel):
    """Internal provenance without URLs, labels, paths, or attachment secrets."""

    model_config = ConfigDict(extra="forbid")

    role: str
    target_id: str
    target_type: str
    status: Literal[InvestigationStatus.ASSERTED] = InvestigationStatus.ASSERTED


class SafeDocumentProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    source_entity_type: str | None = None
    source_system: str | None = None
    asserted_relations: list[SafeProvenanceRelation] = Field(default_factory=list)


class AssociationReadyValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    observation_id: str
    field: str
    source: str
    sensitivity: AssociationValueSensitivity


class AssociationCandidateKey(BaseModel):
    """Opaque bucket key; raw creator/source values never appear in it."""

    model_config = ConfigDict(extra="forbid")

    dimension: AssociationDimension
    value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def canonical_key(self) -> str:
        return f"{self.dimension.value}:{self.value_sha256}"


class DocumentMetadataInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    document_id: str
    profile_id: str
    profile_version: int
    observations: list[MetadataObservation]
    temporal_observations: list[DocumentTemporalObservation]
    conflicts: list[InvestigationMetadataConflict]
    safe_provenance: SafeDocumentProvenance
    association_ready_values: list[AssociationReadyValue]
    association_keys: list[AssociationCandidateKey]


class AssociationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    dimension: AssociationDimension
    status: InvestigationStatus
    left_observation_id: str
    right_observation_id: str
    left_field: str
    right_field: str
    left_source: str
    right_source: str
    comparison_value: str | None = None
    calendar_basis: CalendarBasis | None = None
    normalized_period: str | None = None


class AssociationDimensionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: AssociationDimension
    status: InvestigationStatus
    evidence_ids: list[str]


class DocumentMetadataComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_document_id: str
    right_document_id: str
    temporal_relations: list[TemporalRelation]
    calendar_period_associations: list[CalendarPeriodAssociation]
    dimensions: list[AssociationDimension]
    dimension_results: list[AssociationDimensionResult]
    evidence: list[AssociationEvidence]


class DocumentAssociation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_document_id: str
    right_document_id: str
    dimensions: list[AssociationDimension]
    dimension_results: list[AssociationDimensionResult]
    evidence: list[AssociationEvidence]
    association_strength: AssociationStrength
    association_status: InvestigationStatus
    scope_expanding: Literal[False] = False
    policy_id: Literal["openrag.document-investigation-association"] = (
        "openrag.document-investigation-association"
    )
    policy_version: Literal[1] = 1
    document_association_sha256: str = ""

    @model_validator(mode="after")
    def validate_canonical_association(self) -> DocumentAssociation:
        if self.left_document_id >= self.right_document_id:
            raise ValueError("association endpoints must be distinct and canonically ordered")
        if not self.dimensions:
            if self.association_strength != AssociationStrength.NONE:
                raise ValueError("dimensionless associations must use strength NONE")
            if self.association_status != InvestigationStatus.UNKNOWN:
                raise ValueError("dimensionless associations must use status UNKNOWN")
        elif self.association_strength == AssociationStrength.NONE:
            raise ValueError("associations with dimensions cannot use strength NONE")
        calculated = self.calculate_sha256()
        if self.document_association_sha256 and self.document_association_sha256 != calculated:
            raise ValueError("document_association_sha256 does not match canonical evidence")
        self.document_association_sha256 = calculated
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"document_association_sha256"},
            exclude_none=True,
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def calculate_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class DocumentChronology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    temporal_observations: list[DocumentTemporalObservation]
    comparable_relations: list[TemporalRelation]
    calendar_period_associations: list[CalendarPeriodAssociation]
    conflicts: list[InvestigationMetadataConflict]
    indeterminate_relations: list[TemporalRelation]
    provenance: SafeDocumentProvenance


class DocumentMetadataEvidenceProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    status: InvestigationStatus
    temporal_observations: list[dict[str, str | None]]
    conflicts: list[dict[str, str]]
    truncated: bool


class DocumentAssociationEvidenceProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_document_id: str
    right_document_id: str
    association_strength: AssociationStrength
    association_status: InvestigationStatus
    reasons: list[str]
    truncated: bool
    scope_expanding: Literal[False] = False


class CandidateLineageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_document_id: str
    right_document_id: str
    dimensions: list[AssociationDimension]
    evidence: list[AssociationEvidence]
    status: Literal["candidate_only"] = "candidate_only"
    scope_expanding: Literal[False] = False
    prov_o_edges: Literal[0] = 0


class NeighborhoodLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_documents: int = Field(default=25, ge=1, le=10_000)
    max_associations: int = Field(default=50, ge=0, le=50_000)
    per_dimension_limit: int = Field(default=10, ge=1, le=1_000)
    time_window_days: int | None = Field(default=None, ge=0, le=365_250)
    source_scope: tuple[str, ...] = ()


class NeighborhoodInclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    association_ids: list[str]
    dimensions: list[AssociationDimension]


class DocumentaryNeighborhood(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_document_ids: list[str]
    document_ids: list[str]
    associations: list[DocumentAssociation]
    inclusions: list[NeighborhoodInclusion]
    limits: NeighborhoodLimits
    truncated_dimensions: list[AssociationDimension]
    completeness: Literal[NeighborhoodCompleteness.BOUNDED_NOT_EXHAUSTIVE] = (
        NeighborhoodCompleteness.BOUNDED_NOT_EXHAUSTIVE
    )
    scope_expanding: Literal[False] = False
