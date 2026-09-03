"""Bounded public contract for Agent-initiated structured metadata search.

The Agent can name only versioned metadata-filter fields and a deliberately
small operator set.  It cannot submit OpenSearch/Lucene JSON or change the
three-valued truth, DLS, ranking, or documentary-scope policies.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.document_investigation import CalendarBasis
from models.metadata_filter import (
    TEMPORAL_FILTER_FIELDS,
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterClause,
    MetadataFilterField,
    MetadataFilterOperator,
)

METADATA_AGENT_TOOL_SCHEMA_ID = "openrag.metadata-agent-search"
METADATA_AGENT_TOOL_SCHEMA_VERSION = 1
MAX_AGENT_FILTERS = 8
MAX_AGENT_IN_VALUES = 16
MAX_AGENT_FREE_TEXT_LENGTH = 512
MAX_AGENT_RESULT_LIMIT = 20
MAX_PLANNER_DIAGNOSTICS = 4


class MetadataPlanStatus(StrEnum):
    VALID = "VALID"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"


class MetadataAgentOperator(StrEnum):
    EQUAL = "EQUAL"
    IN = "IN"
    EXISTS = "EXISTS"
    NOT_EXISTS = "NOT_EXISTS"
    NOT_EQUAL = "NOT_EQUAL"


class MetadataAgentFilter(BaseModel):
    """One strict Agent-facing predicate; ``value`` is never arbitrary JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: MetadataFilterField
    operator: MetadataAgentOperator
    value: str | tuple[str, ...] | None = None
    calendar_basis: CalendarBasis | None = None

    @model_validator(mode="after")
    def validate_value_shape(self) -> MetadataAgentFilter:
        if self.operator in {MetadataAgentOperator.EXISTS, MetadataAgentOperator.NOT_EXISTS}:
            if self.value is not None:
                raise ValueError(f"{self.operator.value} does not accept a value")
        elif self.operator == MetadataAgentOperator.IN:
            if not isinstance(self.value, tuple) or not self.value:
                raise ValueError("IN requires a non-empty array value")
            if len(self.value) > MAX_AGENT_IN_VALUES:
                raise ValueError(f"IN supports at most {MAX_AGENT_IN_VALUES} values")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError(f"{self.operator.value} requires one non-blank string value")

        values = self.value if isinstance(self.value, tuple) else (self.value,)
        if any(isinstance(item, str) and len(item) > 256 for item in values):
            raise ValueError("metadata filter values must not exceed 256 characters")
        if self.field in TEMPORAL_FILTER_FIELDS:
            if self.calendar_basis is None:
                raise ValueError("temporal Agent filters require an explicit calendar_basis")
        elif self.calendar_basis is not None:
            raise ValueError("calendar_basis is only valid for temporal fields")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        if self.operator == MetadataAgentOperator.IN:
            payload["value"] = sorted(set(payload["value"]))
        return payload


class MetadataAgentQuery(BaseModel):
    """Complete, bounded input accepted by ``document_search_with_metadata``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    free_text: str = Field(min_length=1, max_length=MAX_AGENT_FREE_TEXT_LENGTH)
    filters: tuple[MetadataAgentFilter, ...] = Field(min_length=1, max_length=MAX_AGENT_FILTERS)
    limit: int = Field(default=10, ge=1, le=MAX_AGENT_RESULT_LIMIT)
    schema_id: Literal["openrag.metadata-agent-search"] = "openrag.metadata-agent-search"
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def reject_blank_free_text(self) -> MetadataAgentQuery:
        if not self.free_text.strip():
            raise ValueError("free_text must not be blank")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "filters": [
                item.canonical_payload()
                for item in sorted(
                    self.filters,
                    key=lambda value: json.dumps(
                        value.canonical_payload(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            ],
            "free_text": self.free_text.strip(),
            "limit": self.limit,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


class NaturalLanguageMetadataPlan(BaseModel):
    """Deterministic proposal supplied to Langflow as request-scoped context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MetadataPlanStatus
    free_text: str = Field(default="", max_length=MAX_AGENT_FREE_TEXT_LENGTH)
    filters: tuple[MetadataAgentFilter, ...] = Field(default=(), max_length=MAX_AGENT_FILTERS)
    ambiguities: tuple[str, ...] = Field(default=(), max_length=MAX_PLANNER_DIAGNOSTICS)
    unsupported_constraints: tuple[str, ...] = Field(
        default=(), max_length=MAX_PLANNER_DIAGNOSTICS
    )
    metadata_intent_detected: bool = False
    requires_metadata_search: bool = False
    planner_mode: Literal["DETERMINISTIC_ONLY"] = "DETERMINISTIC_ONLY"
    schema_id: Literal["openrag.metadata-natural-language-plan"] = (
        "openrag.metadata-natural-language-plan"
    )
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def validate_state(self) -> NaturalLanguageMetadataPlan:
        if self.status == MetadataPlanStatus.VALID:
            if self.ambiguities or self.unsupported_constraints:
                raise ValueError("VALID plans cannot contain blocking diagnostics")
            if self.requires_metadata_search != bool(self.filters):
                raise ValueError("requires_metadata_search must match the presence of filters")
        elif self.requires_metadata_search:
            raise ValueError("blocked plans cannot request metadata search")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["filters"] = [item.canonical_payload() for item in self.filters]
        return payload

    def calculate_sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_agent_query(query: MetadataAgentQuery) -> MetadataFilter:
    """Compile the bounded tool schema to existing v1 primitives only."""

    clauses: list[MetadataFilterClause] = []
    for item in query.filters:
        values = item.value if isinstance(item.value, tuple) else (item.value,)
        normalized_values = tuple(value for value in values if isinstance(value, str))
        operator = {
            MetadataAgentOperator.EQUAL: MetadataFilterOperator.EQUAL,
            MetadataAgentOperator.IN: MetadataFilterOperator.IN,
            MetadataAgentOperator.EXISTS: MetadataFilterOperator.EXISTS,
            MetadataAgentOperator.NOT_EXISTS: MetadataFilterOperator.NOT_EXISTS,
            MetadataAgentOperator.NOT_EQUAL: MetadataFilterOperator.EQUAL,
        }[item.operator]
        temporal_role = item.field.value.partition("_")[0]
        source_policy = (
            MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION
            if temporal_role == "production"
            else MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION
            if temporal_role == "modification"
            else None
        )
        clauses.append(
            MetadataFilterClause(
                field=item.field,
                operator=operator,
                values=normalized_values,
                calendar_basis=item.calendar_basis,
                source_policy=source_policy,
                negated=item.operator == MetadataAgentOperator.NOT_EQUAL,
            )
        )
    return MetadataFilter(clauses=tuple(clauses))
