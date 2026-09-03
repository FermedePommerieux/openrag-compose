"""Structured document discovery without natural-language metadata inference."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.metadata_filter import MetadataFilter


class StructuredDocumentQuery(BaseModel):
    """Keep free text and explicit metadata constraints as separate inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    free_text: str = Field(default="", max_length=32_768)
    metadata_filter: MetadataFilter | None = None
    contract: Literal["openrag.structured-document-query"] = "openrag.structured-document-query"
    version: Literal[1] = 1

    @model_validator(mode="after")
    def require_discovery_input(self) -> StructuredDocumentQuery:
        if not self.free_text.strip():
            raise ValueError("free_text is required for retrieval")
        return self

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract": self.contract,
            "free_text": self.free_text,
            "version": self.version,
        }
        if self.metadata_filter is not None:
            payload["metadata_filter"] = self.metadata_filter.canonical_payload()
        return payload

    def calculate_sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class MetadataCandidateDiagnostics(BaseModel):
    """DLS-scoped diagnostics; never contains global/hidden corpus counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filters_requested: int = Field(ge=1)
    filters_effective: int = Field(ge=1)
    filters_unsupported: tuple[str, ...] = ()
    filters_ambiguous: tuple[str, ...] = ()
    visible_projection_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    pages: int = Field(ge=0)
    truncated: bool = False
    resolution_seconds: float = Field(default=0.0, ge=0)


class MetadataCandidateRestriction(BaseModel):
    """Exact visible occurrence ids selected by a metadata projection query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_entity_ids: tuple[str, ...]
    diagnostics: MetadataCandidateDiagnostics
    projection_alias: str

    @model_validator(mode="after")
    def canonical_ids(self) -> MetadataCandidateRestriction:
        if self.source_entity_ids != tuple(sorted(set(self.source_entity_ids))):
            raise ValueError("metadata candidate ids must be sorted and unique")
        if len(self.source_entity_ids) != self.diagnostics.eligible_count:
            raise ValueError("eligible_count differs from candidate identity count")
        return self
