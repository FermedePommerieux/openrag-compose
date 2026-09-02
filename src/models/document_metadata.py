"""Versioned, observational document metadata with per-value provenance.

The profile deliberately stays separate from retrieval text and vectors.  It
describes the existing ``SourceProvenance.entity``; it does not create a second
document identity and it never creates graph edges by itself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOCUMENT_METADATA_PROFILE_ID = "openrag.document-metadata"
DOCUMENT_METADATA_PROFILE_VERSION = 1
DOCUMENT_METADATA_NORMALIZATION_ID = "openrag.document-metadata-normalization"
DOCUMENT_METADATA_NORMALIZATION_VERSION = 1
METADATA_RESOLUTION_POLICY_ID = "openrag.metadata-resolution"
METADATA_RESOLUTION_POLICY_VERSION = 1
DOCUMENT_METADATA_EXTRACTOR_NAME = "openrag-native-metadata"
DOCUMENT_METADATA_EXTRACTOR_VERSION = "1.0.0"


class MetadataTrustClass(StrEnum):
    """Origin class, never a claim that an observed value is true."""

    ARCHIVE_SYSTEM = "archive_system"
    EMBEDDED_DOCUMENT_METADATA = "embedded_document_metadata"
    FILESYSTEM_METADATA = "filesystem_metadata"
    INGESTION_SYSTEM = "ingestion_system"
    DERIVED_METADATA = "derived_metadata"
    INFERRED_METADATA = "inferred_metadata"


class MetadataSourceType(StrEnum):
    FORMAT_NATIVE = "format_native"
    ARCHIVE_NATIVE = "archive_native"
    FILESYSTEM = "filesystem"
    INGESTION = "ingestion"
    DERIVED = "derived"
    INFERRED = "inferred"
    PARENT_CONTEXT = "parent_context"


class MetadataExposureClass(StrEnum):
    """Maximum v1 projection surface for one fact."""

    INTERNAL = "internal_metadata"
    RETRIEVAL_VISIBLE = "retrieval_visible_metadata"
    MODEL_VISIBLE = "model_visible_metadata"
    PUBLIC_API = "public_api_metadata"


class MetadataNormalizationStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NORMALIZED = "normalized"
    TIMEZONE_EXPLICIT = "timezone_explicit"
    TIMEZONE_UNKNOWN = "timezone_unknown"
    INVALID = "invalid"
    REDACTED_SENSITIVE = "redacted_sensitive"


class MetadataSectionName(StrEnum):
    IDENTITY = "identity"
    EMBEDDED = "embedded"
    FILESYSTEM = "filesystem"
    ARCHIVE = "archive"
    INGESTION = "ingestion"


class MetadataObservation(BaseModel):
    """One source-qualified observation; conflicting values coexist."""

    model_config = ConfigDict(extra="forbid")

    section: MetadataSectionName
    field: str = Field(min_length=1, max_length=128)
    value: str | int | float | bool | list[str] | None = None
    raw_value: str | int | float | bool | list[str] | None = None
    source: str = Field(min_length=1, max_length=256)
    source_type: MetadataSourceType
    trust_class: MetadataTrustClass
    exposure_class: MetadataExposureClass = MetadataExposureClass.INTERNAL
    extracted_at: datetime
    normalization_id: Literal["openrag.document-metadata-normalization"] = (
        "openrag.document-metadata-normalization"
    )
    normalization_version: Literal[1] = 1
    normalization_status: MetadataNormalizationStatus = MetadataNormalizationStatus.NOT_APPLICABLE
    timezone: str | None = Field(default=None, max_length=128)

    @field_validator("field", "source")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("metadata field/source must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_timezone_contract(self) -> MetadataObservation:
        timestamp_statuses = {
            MetadataNormalizationStatus.TIMEZONE_EXPLICIT,
            MetadataNormalizationStatus.TIMEZONE_UNKNOWN,
            MetadataNormalizationStatus.INVALID,
        }
        if self.normalization_status in timestamp_statuses and self.raw_value is None:
            raise ValueError("timestamp observations must preserve raw_value")
        if self.normalization_status == MetadataNormalizationStatus.TIMEZONE_UNKNOWN:
            if self.timezone != "UNKNOWN":
                raise ValueError("timezone-less timestamps must say timezone=UNKNOWN")
        if self.normalization_status == MetadataNormalizationStatus.TIMEZONE_EXPLICIT:
            if not self.timezone or self.timezone == "UNKNOWN":
                raise ValueError("explicit timestamp timezone must be preserved")
        return self

    def canonical_fact(self) -> dict[str, Any]:
        """Return the stable fact material, excluding extraction wall time."""
        return self.model_dump(mode="json", exclude={"extracted_at"}, exclude_none=True)


class MetadataConflict(BaseModel):
    """A disagreement retained for review, without silently selecting a truth."""

    model_config = ConfigDict(extra="forbid")

    field: str
    values: list[str]
    sources: list[str]
    resolution: Literal["unresolved_observations_preserved"] = "unresolved_observations_preserved"


class DocumentMetadataProfile(BaseModel):
    """Metadata attached to an existing source entity/occurrence."""

    model_config = ConfigDict(extra="forbid")

    profile_id: Literal["openrag.document-metadata"] = "openrag.document-metadata"
    profile_version: Literal[1] = 1
    entity_id: str = Field(min_length=1, max_length=1024)
    identity: list[MetadataObservation] = Field(default_factory=list)
    embedded: list[MetadataObservation] = Field(default_factory=list)
    filesystem: list[MetadataObservation] = Field(default_factory=list)
    archive: list[MetadataObservation] = Field(default_factory=list)
    ingestion: list[MetadataObservation] = Field(default_factory=list)
    conflicts: list[MetadataConflict] = Field(default_factory=list)
    extractor_name: Literal["openrag-native-metadata"] = "openrag-native-metadata"
    extractor_version: Literal["1.0.0"] = "1.0.0"
    resolution_policy_id: Literal["openrag.metadata-resolution"] = "openrag.metadata-resolution"
    resolution_policy_version: Literal[1] = 1
    metadata_facts_sha256: str = ""

    @model_validator(mode="after")
    def validate_sections_and_digest(self) -> DocumentMetadataProfile:
        for section_name in MetadataSectionName:
            observations = getattr(self, section_name.value)
            if any(item.section != section_name for item in observations):
                raise ValueError(f"metadata observations must remain in {section_name.value}")
        calculated = self.calculate_facts_sha256()
        if self.metadata_facts_sha256 and self.metadata_facts_sha256 != calculated:
            raise ValueError("metadata_facts_sha256 does not match canonical facts")
        self.metadata_facts_sha256 = calculated
        return self

    def observations(self) -> list[MetadataObservation]:
        return [
            *self.identity,
            *self.embedded,
            *self.filesystem,
            *self.archive,
            *self.ingestion,
        ]

    def calculate_facts_sha256(self) -> str:
        """Hash canonical source-qualified facts, not volatile extraction time."""
        facts = sorted(
            (item.canonical_fact() for item in self.observations()),
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        material = {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "entity_id": self.entity_id,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "resolution_policy_id": self.resolution_policy_id,
            "resolution_policy_version": self.resolution_policy_version,
            "facts": facts,
            "conflicts": sorted(
                (item.model_dump(mode="json") for item in self.conflicts),
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def document_metadata_mapping() -> dict[str, Any]:
    """Additive v1 mapping with the profile stored but not indexed for ranking.

    Individual metadata values are intentionally unavailable to BM25, dense
    search, filters, aggregations, and scripts in v1.  A later chantier may add
    selected projections after a separate exposure and ranking review.
    """

    return {
        "document_metadata_profile": {"type": "object", "enabled": False},
        "document_metadata_profile_id": {"type": "keyword"},
        "document_metadata_profile_version": {"type": "integer"},
        "document_metadata_facts_sha256": {"type": "keyword"},
        "document_metadata_extractor": {"type": "keyword"},
        "document_metadata_extractor_version": {"type": "keyword"},
        "document_metadata_backfill_status": {"type": "keyword"},
        "document_metadata_updated_at": {"type": "date"},
    }
