"""Deterministic, rebuildable metadata-filter projection contract.

``openrag.metadata-filter-projection v1`` is derived from the complete raw
metadata profile plus explicitly supplied indexed source context.  It never
replaces or mutates the raw profile and never chooses a preferred observation.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.document_investigation import InvestigationStatus, InvestigationTimezoneStatus

METADATA_FILTER_PROJECTION_ID = "openrag.metadata-filter-projection"
METADATA_FILTER_PROJECTION_VERSION = 1
METADATA_FILTER_PROJECTION_FIELD = "filter"


class MetadataProjectionObservationSource(StrEnum):
    PDF_INFO = "pdf_info"
    PDF_XMP = "pdf_xmp"
    OOXML_CORE = "ooxml_core"
    EML_HEADER = "eml_header"
    EXIF = "exif"
    XMP = "xmp"
    ARCHIVE = "archive"
    FILESYSTEM = "filesystem"
    INGESTION = "ingestion"
    OTHER_FORMAT_NATIVE = "other_format_native"


class ProjectedTemporalObservation(BaseModel):
    """Source-qualified temporal evidence without raw/private source values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["production", "modification"]
    field: str = Field(min_length=1, max_length=128)
    source: MetadataProjectionObservationSource
    status: InvestigationStatus
    timezone_status: InvestigationTimezoneStatus
    source_local_day: str | None = None
    source_local_month: str | None = None
    source_local_year: str | None = None
    utc_day: str | None = None
    utc_month: str | None = None
    utc_year: str | None = None

    @field_validator("source_local_day", "utc_day")
    @classmethod
    def validate_day(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", value) is None:
            raise ValueError("projected day must use YYYY-MM-DD")
        return value

    @field_validator("source_local_month", "utc_month")
    @classmethod
    def validate_month(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"^\d{4}-\d{2}$", value) is None:
            raise ValueError("projected month must use YYYY-MM")
        return value

    @field_validator("source_local_year", "utc_year")
    @classmethod
    def validate_year(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"^\d{4}$", value) is None:
            raise ValueError("projected year must use YYYY")
        return value


class ProjectedValueObservation(BaseModel):
    """Stored, unindexed evidence for one flattened exact-match value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    field: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=256)
    normalized_value: str = Field(min_length=1, max_length=4096)
    status: InvestigationStatus


class MetadataFilterProjectionSourceContext(BaseModel):
    """Complete non-profile facts used to build one occurrence projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_entity_id: str | None = Field(default=None, max_length=4096)
    source_entity_type: str | None = Field(default=None, max_length=512)
    source_system: str | None = Field(default=None, max_length=512)
    connector: str | None = Field(default=None, max_length=512)
    mime_type: str | None = Field(default=None, max_length=512)
    filename: str | None = Field(default=None, max_length=4096)

    def canonical_payload(self) -> dict[str, str]:
        return {
            key: value.strip()
            for key, value in self.model_dump(exclude_none=True).items()
            if value.strip()
        }

    def calculate_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class MetadataFilterProjection(BaseModel):
    """Canonical filter fields for one complete v1 metadata profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["openrag.metadata-filter-projection"] = (
        "openrag.metadata-filter-projection"
    )
    projection_version: Literal[1] = 1
    source_metadata_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_sha256: str = ""

    production_day_local: tuple[str, ...] = ()
    production_month_local: tuple[str, ...] = ()
    production_year_local: tuple[str, ...] = ()
    production_day_utc: tuple[str, ...] = ()
    production_month_utc: tuple[str, ...] = ()
    production_year_utc: tuple[str, ...] = ()
    modification_day_local: tuple[str, ...] = ()
    modification_month_local: tuple[str, ...] = ()
    modification_year_local: tuple[str, ...] = ()
    modification_day_utc: tuple[str, ...] = ()
    modification_month_utc: tuple[str, ...] = ()
    modification_year_utc: tuple[str, ...] = ()

    has_production_observation: bool
    has_valid_production_observation: bool
    has_modification_observation: bool
    has_valid_modification_observation: bool
    has_timezone_unknown: bool
    has_invalid_timestamp: bool
    has_temporal_conflict: bool

    production_observation_sources: tuple[str, ...] = ()
    modification_observation_sources: tuple[str, ...] = ()
    temporal_observations: tuple[ProjectedTemporalObservation, ...] = ()

    mime_types: tuple[str, ...] = ()
    format_families: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    explicit_document_types: tuple[str, ...] = ()

    source_systems: tuple[str, ...] = ()
    source_entity_types: tuple[str, ...] = ()
    source_entity_families: tuple[str, ...] = ()
    source_connectors: tuple[str, ...] = ()
    parent_collection_ids_safe: tuple[str, ...] = ()

    creator_normalized: tuple[str, ...] = ()
    last_modifier_normalized: tuple[str, ...] = ()
    producer_normalized: tuple[str, ...] = ()
    creator_application_normalized: tuple[str, ...] = ()

    filename_basename_normalized: tuple[str, ...] = ()
    binary_sha256: tuple[str, ...] = ()

    has_metadata_conflict: bool
    conflict_types: tuple[str, ...] = ()
    value_observations: tuple[ProjectedValueObservation, ...] = ()

    @field_validator(
        "production_day_local",
        "production_month_local",
        "production_year_local",
        "production_day_utc",
        "production_month_utc",
        "production_year_utc",
        "modification_day_local",
        "modification_month_local",
        "modification_year_local",
        "modification_day_utc",
        "modification_month_utc",
        "modification_year_utc",
        "production_observation_sources",
        "modification_observation_sources",
        "mime_types",
        "format_families",
        "extensions",
        "explicit_document_types",
        "source_systems",
        "source_entity_types",
        "source_entity_families",
        "source_connectors",
        "parent_collection_ids_safe",
        "creator_normalized",
        "last_modifier_normalized",
        "producer_normalized",
        "creator_application_normalized",
        "filename_basename_normalized",
        "binary_sha256",
        "conflict_types",
    )
    @classmethod
    def require_canonical_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("projection arrays must be sorted and unique")
        if any(not item for item in value):
            raise ValueError("projection arrays cannot contain empty values")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> MetadataFilterProjection:
        period_patterns = {
            "_day_": r"^\d{4}-\d{2}-\d{2}$",
            "_month_": r"^\d{4}-\d{2}$",
            "_year_": r"^\d{4}$",
        }
        for field_name, pattern in period_patterns.items():
            for model_field in type(self).model_fields:
                if field_name in model_field:
                    if any(re.fullmatch(pattern, item) is None for item in getattr(self, model_field)):
                        raise ValueError(f"invalid period in {model_field}")
        if any(re.fullmatch(r"^[0-9a-f]{64}$", item) is None for item in self.binary_sha256):
            raise ValueError("binary_sha256 values must be complete lowercase SHA-256")
        calculated = self.calculate_sha256()
        if self.projection_sha256 and self.projection_sha256 != calculated:
            raise ValueError("projection_sha256 does not match canonical projection")
        object.__setattr__(self, "projection_sha256", calculated)
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"projection_sha256"},
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
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class MetadataFilterProjectionSideDocument(BaseModel):
    """One DLS-equivalent side-index row for a source occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_document_id: str = Field(min_length=1, max_length=1024)
    source_entity_id: str = Field(min_length=1, max_length=4096)
    representative_chunk_id: str = Field(min_length=1, max_length=1024)
    owner: str | None = Field(default=None, max_length=1024)
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    allowed_principals: tuple[str, ...] = ()
    filter: MetadataFilterProjection

    @field_validator("allowed_users", "allowed_groups", "allowed_principals")
    @classmethod
    def canonical_acl(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("DLS arrays must be sorted and unique")
        return value


def metadata_filter_projection_mapping() -> dict[str, Any]:
    """Strict mapping for the recommended isolated projection side index."""
    keyword = {"type": "keyword", "ignore_above": 4096}
    period_day = {"type": "date", "format": "strict_date"}
    period_keyword = {"type": "keyword"}
    temporal_properties: dict[str, Any] = {
        "observation_id": {"type": "keyword"},
        "role": {"type": "keyword"},
        "field": {"type": "keyword"},
        "source": {"type": "keyword"},
        "status": {"type": "keyword"},
        "timezone_status": {"type": "keyword"},
        "source_local_day": period_day,
        "source_local_month": period_keyword,
        "source_local_year": period_keyword,
        "utc_day": period_day,
        "utc_month": period_keyword,
        "utc_year": period_keyword,
    }
    filter_properties: dict[str, Any] = {
        "contract": {"type": "keyword"},
        "projection_version": {"type": "integer"},
        "source_metadata_facts_sha256": {"type": "keyword"},
        "source_context_sha256": {"type": "keyword"},
        "projection_sha256": {"type": "keyword"},
        "production_day_local": period_day,
        "production_month_local": period_keyword,
        "production_year_local": period_keyword,
        "production_day_utc": period_day,
        "production_month_utc": period_keyword,
        "production_year_utc": period_keyword,
        "modification_day_local": period_day,
        "modification_month_local": period_keyword,
        "modification_year_local": period_keyword,
        "modification_day_utc": period_day,
        "modification_month_utc": period_keyword,
        "modification_year_utc": period_keyword,
        "has_production_observation": {"type": "boolean"},
        "has_valid_production_observation": {"type": "boolean"},
        "has_modification_observation": {"type": "boolean"},
        "has_valid_modification_observation": {"type": "boolean"},
        "has_timezone_unknown": {"type": "boolean"},
        "has_invalid_timestamp": {"type": "boolean"},
        "has_temporal_conflict": {"type": "boolean"},
        "production_observation_sources": {"type": "keyword"},
        "modification_observation_sources": {"type": "keyword"},
        "temporal_observations": {
            "type": "nested",
            "dynamic": "strict",
            "properties": temporal_properties,
        },
        "mime_types": keyword,
        "format_families": keyword,
        "extensions": keyword,
        "explicit_document_types": keyword,
        "source_systems": keyword,
        "source_entity_types": keyword,
        # Populated only by an explicit metadata observation.  It is never
        # inferred from source_entity_types.
        "source_entity_families": keyword,
        "source_connectors": keyword,
        "parent_collection_ids_safe": {"type": "keyword"},
        "creator_normalized": keyword,
        "last_modifier_normalized": keyword,
        "producer_normalized": keyword,
        "creator_application_normalized": keyword,
        "filename_basename_normalized": keyword,
        "binary_sha256": {"type": "keyword"},
        "has_metadata_conflict": {"type": "boolean"},
        "conflict_types": {"type": "keyword"},
        # Evidence is returned only after DLS-scoped candidate selection.  It
        # is deliberately not searchable or aggregatable.
        "value_observations": {"type": "object", "enabled": False},
    }
    return {
        "dynamic": "strict",
        "properties": {
            "projection_document_id": {"type": "keyword"},
            "source_document_id": {"type": "keyword"},
            "source_entity_id": keyword,
            "representative_chunk_id": {"type": "keyword"},
            "owner": {"type": "keyword"},
            "allowed_users": {"type": "keyword"},
            "allowed_groups": {"type": "keyword"},
            "allowed_principals": {"type": "keyword"},
            METADATA_FILTER_PROJECTION_FIELD: {
                "type": "object",
                "dynamic": "strict",
                "properties": filter_properties,
            },
        },
    }


def metadata_filter_projection_index_body(
    *, number_of_shards: int = 1, number_of_replicas: int = 0
) -> dict[str, Any]:
    if number_of_shards < 1 or number_of_replicas < 0:
        raise ValueError("invalid side-index shard configuration")
    return {
        "settings": {
            "index": {
                "number_of_shards": number_of_shards,
                "number_of_replicas": number_of_replicas,
            }
        },
        "mappings": metadata_filter_projection_mapping(),
    }
