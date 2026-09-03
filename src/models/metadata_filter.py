"""Versioned, internal metadata-filter contract with three-valued logic.

The contract is intentionally detached from search and OpenSearch.  It
evaluates source-qualified observations, never manufactures a preferred
timestamp, and keeps unavailable metadata distinct from a negative fact.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.document_investigation import (
    CalendarBasis,
    InvestigationStatus,
    InvestigationTimezoneStatus,
)

METADATA_FILTER_POLICY_ID = "openrag.metadata-filter"
METADATA_FILTER_POLICY_VERSION = 1


class MetadataTruthValue(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class MetadataFilterOperator(StrEnum):
    EQUAL = "EQUAL"
    IN = "IN"
    BETWEEN = "BETWEEN"
    BEFORE = "BEFORE"
    AFTER = "AFTER"
    EXISTS = "EXISTS"
    NOT_EXISTS = "NOT_EXISTS"


class MetadataFilterConjunction(StrEnum):
    ALL = "ALL"
    ANY = "ANY"


class MetadataFilterBooleanOperator(StrEnum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"


class MetadataDateSourcePolicy(StrEnum):
    ANY_VALID_PRODUCTION_OBSERVATION = "ANY_VALID_PRODUCTION_OBSERVATION"
    ANY_VALID_MODIFICATION_OBSERVATION = "ANY_VALID_MODIFICATION_OBSERVATION"
    EXPLICIT_SOURCE = "EXPLICIT_SOURCE"


class MetadataProfileAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    EXTRACTION_IMPOSSIBLE = "EXTRACTION_IMPOSSIBLE"
    ARCHIVE_SOURCE_UNAVAILABLE = "ARCHIVE_SOURCE_UNAVAILABLE"


class MetadataFilterField(StrEnum):
    PRODUCTION_DAY = "production_day"
    PRODUCTION_MONTH = "production_month"
    PRODUCTION_YEAR = "production_year"
    MODIFICATION_DAY = "modification_day"
    MODIFICATION_MONTH = "modification_month"
    MODIFICATION_YEAR = "modification_year"
    MIME = "mime"
    FORMAT_FAMILY = "format_family"
    EXTENSION = "extension"
    SOURCE_DOCUMENT_TYPE = "source_document_type"
    SOURCE_SYSTEM = "source_system"
    SOURCE_ENTITY_TYPE = "source_entity_type"
    SOURCE_ENTITY_FAMILY = "source_entity_family"
    PARENT_COLLECTION = "parent_collection"
    CONNECTOR = "connector"
    CREATOR_OBSERVATION = "creator_observation"
    LAST_MODIFIER_OBSERVATION = "last_modifier_observation"
    PRODUCER_OBSERVATION = "producer_observation"
    CREATOR_APPLICATION_OBSERVATION = "creator_application_observation"
    FILENAME_BASENAME = "filename_basename"
    BINARY_SHA256 = "binary_sha256"
    HAS_TEMPORAL_CONFLICT = "has_temporal_conflict"
    HAS_METADATA_CONFLICT = "has_metadata_conflict"


TEMPORAL_FILTER_FIELDS = frozenset(
    {
        MetadataFilterField.PRODUCTION_DAY,
        MetadataFilterField.PRODUCTION_MONTH,
        MetadataFilterField.PRODUCTION_YEAR,
        MetadataFilterField.MODIFICATION_DAY,
        MetadataFilterField.MODIFICATION_MONTH,
        MetadataFilterField.MODIFICATION_YEAR,
    }
)


class MetadataFilterContextValue(BaseModel):
    """One independently available structural value supplied by the caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: MetadataFilterField
    value: str = Field(min_length=1, max_length=4096)
    source: str = Field(min_length=1, max_length=256)


class MetadataFilterDocumentContext(BaseModel):
    """DLS-scoped structural facts that do not depend on metadata extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    values: tuple[MetadataFilterContextValue, ...] = ()
    complete_fields: frozenset[MetadataFilterField] = frozenset()

    @model_validator(mode="after")
    def values_require_complete_fields(self) -> MetadataFilterDocumentContext:
        if any(item.field not in self.complete_fields for item in self.values):
            raise ValueError("context values require their field to be declared complete")
        if any(item.field in TEMPORAL_FILTER_FIELDS for item in self.values):
            raise ValueError("temporal observations cannot be supplied as structural context")
        return self


class MetadataFilterClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: MetadataFilterField
    operator: MetadataFilterOperator
    values: tuple[str, ...] = ()
    calendar_basis: CalendarBasis | None = None
    source_policy: MetadataDateSourcePolicy | None = None
    explicit_source: str | None = Field(default=None, min_length=1, max_length=256)
    negated: bool = False

    @model_validator(mode="after")
    def validate_clause(self) -> MetadataFilterClause:
        expected_values = {
            MetadataFilterOperator.EQUAL: (1, 1),
            MetadataFilterOperator.IN: (1, 256),
            MetadataFilterOperator.BETWEEN: (2, 2),
            MetadataFilterOperator.BEFORE: (1, 1),
            MetadataFilterOperator.AFTER: (1, 1),
            MetadataFilterOperator.EXISTS: (0, 0),
            MetadataFilterOperator.NOT_EXISTS: (0, 0),
        }[self.operator]
        if not expected_values[0] <= len(self.values) <= expected_values[1]:
            raise ValueError(
                f"{self.operator.value} requires between {expected_values[0]} "
                f"and {expected_values[1]} values"
            )
        if any(not value.strip() for value in self.values):
            raise ValueError("metadata filter values must not be blank")

        temporal = self.field in TEMPORAL_FILTER_FIELDS
        if temporal:
            if self.calendar_basis is None:
                raise ValueError("temporal filters require an explicit calendar_basis")
            role = self.field.value.partition("_")[0]
            allowed_role_policy = {
                "production": MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
                "modification": MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION,
            }[role]
            if self.source_policy is None:
                raise ValueError("temporal filters require an explicit source_policy")
            if self.source_policy not in {
                allowed_role_policy,
                MetadataDateSourcePolicy.EXPLICIT_SOURCE,
            }:
                raise ValueError("date source policy does not match the temporal role")
            if self.source_policy == MetadataDateSourcePolicy.EXPLICIT_SOURCE:
                if self.explicit_source is None:
                    raise ValueError("EXPLICIT_SOURCE requires explicit_source")
            elif self.explicit_source is not None:
                raise ValueError("explicit_source is only valid with EXPLICIT_SOURCE")
            self._validate_temporal_values()
        elif any(
            value is not None
            for value in (self.calendar_basis, self.source_policy, self.explicit_source)
        ):
            raise ValueError("non-temporal filters cannot declare date source options")

        if (
            self.operator
            in {
                MetadataFilterOperator.BETWEEN,
                MetadataFilterOperator.BEFORE,
                MetadataFilterOperator.AFTER,
            }
            and not temporal
        ):
            raise ValueError(f"{self.operator.value} is limited to temporal fields in v1")
        if self.operator == MetadataFilterOperator.BETWEEN and self.values[0] > self.values[1]:
            raise ValueError("BETWEEN lower bound must not exceed upper bound")
        return self

    def _validate_temporal_values(self) -> None:
        if self.operator in {MetadataFilterOperator.EXISTS, MetadataFilterOperator.NOT_EXISTS}:
            return
        granularity = self.field.value.rpartition("_")[2]
        pattern = {
            "day": r"^\d{4}-\d{2}-\d{2}$",
            "month": r"^\d{4}-\d{2}$",
            "year": r"^\d{4}$",
        }[granularity]
        if any(re.fullmatch(pattern, value) is None for value in self.values):
            raise ValueError(f"values do not match the granularity of {self.field.value}")
        try:
            for value in self.values:
                if granularity == "day":
                    datetime.strptime(value, "%Y-%m-%d")
                elif granularity == "month":
                    datetime.strptime(f"{value}-01", "%Y-%m-%d")
                elif not 1 <= int(value) <= 9999:
                    raise ValueError
        except ValueError as exc:
            raise ValueError(
                f"values are not valid calendar periods for {self.field.value}"
            ) from exc

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        if self.operator == MetadataFilterOperator.IN:
            payload["values"] = sorted(set(payload["values"]))
        return payload

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def calculate_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class MetadataFilterExpression(BaseModel):
    """Recursive AND/OR/NOT expression; leaves contain one v1 clause.

    The original flat ``clauses + conjunction`` shape remains valid.  This is
    an additive v1 representation for structured callers that need explicit
    nesting, especially NOT over a compound expression.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operator: MetadataFilterBooleanOperator | None = None
    clause: MetadataFilterClause | None = None
    children: tuple[MetadataFilterExpression, ...] = ()

    @model_validator(mode="after")
    def validate_expression(self) -> MetadataFilterExpression:
        if self.clause is not None:
            if self.operator is not None or self.children:
                raise ValueError("a clause expression cannot also have an operator or children")
            return self
        if self.operator is None:
            raise ValueError("an expression requires either a clause or an operator")
        expected = (1, 1) if self.operator == MetadataFilterBooleanOperator.NOT else (2, 32)
        if not expected[0] <= len(self.children) <= expected[1]:
            raise ValueError(
                f"{self.operator.value} requires between {expected[0]} and "
                f"{expected[1]} children"
            )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        if self.clause is not None:
            return {"clause": self.clause.canonical_payload()}
        children = [child.canonical_payload() for child in self.children]
        if self.operator in {
            MetadataFilterBooleanOperator.AND,
            MetadataFilterBooleanOperator.OR,
        }:
            children.sort(
                key=lambda value: json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return {"children": children, "operator": self.operator.value}

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def calculate_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class MetadataFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clauses: tuple[MetadataFilterClause, ...] = Field(default=(), max_length=32)
    conjunction: MetadataFilterConjunction = MetadataFilterConjunction.ALL
    expression: MetadataFilterExpression | None = None
    policy_id: Literal["openrag.metadata-filter"] = "openrag.metadata-filter"
    policy_version: Literal[1] = 1

    @model_validator(mode="after")
    def validate_filter_shape(self) -> MetadataFilter:
        if bool(self.clauses) == bool(self.expression):
            raise ValueError("provide exactly one of clauses or expression")
        if self.expression is not None and self.conjunction != MetadataFilterConjunction.ALL:
            raise ValueError("conjunction only applies to the legacy flat clauses shape")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        if self.expression is not None:
            return {
                "expression": self.expression.canonical_payload(),
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
            }
        return {
            "clauses": [
                clause.canonical_payload()
                for clause in sorted(self.clauses, key=lambda item: item.canonical_json())
            ],
            "conjunction": self.conjunction.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def calculate_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class MetadataFilterObservationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    field: str
    source: str
    normalized_value: str | None = None
    status: InvestigationStatus
    timezone_status: InvestigationTimezoneStatus | None = None


class MetadataFilterClauseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clause_sha256: str
    result: MetadataTruthValue
    matched_observations: tuple[MetadataFilterObservationEvidence, ...] = ()
    conflicting_observations: tuple[MetadataFilterObservationEvidence, ...] = ()
    policy_id: Literal["openrag.metadata-filter"] = "openrag.metadata-filter"
    policy_version: Literal[1] = 1


class MetadataFilterEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    filter_sha256: str
    result: MetadataTruthValue
    clause_evaluations: tuple[MetadataFilterClauseEvaluation, ...]
    matched_observations: tuple[MetadataFilterObservationEvidence, ...] = ()
    conflicting_observations: tuple[MetadataFilterObservationEvidence, ...] = ()
    profile_availability: MetadataProfileAvailability
    policy_id: Literal["openrag.metadata-filter"] = "openrag.metadata-filter"
    policy_version: Literal[1] = 1

    @property
    def matches(self) -> bool:
        """Fail closed: only an explicit TRUE is eligible."""
        return self.result == MetadataTruthValue.TRUE


class MetadataProjectionFilterEvaluation(BaseModel):
    """Evaluation evidence returned from an indexed v1 projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    filter_sha256: str
    result: MetadataTruthValue
    matched_fields: tuple[str, ...] = ()
    matched_observations: tuple[MetadataFilterObservationEvidence, ...] = ()
    conflict_flags: tuple[str, ...] = ()
    projection_version: Literal[1] = 1
    policy_id: Literal["openrag.metadata-filter"] = "openrag.metadata-filter"
    policy_version: Literal[1] = 1

    @property
    def matches(self) -> bool:
        return self.result == MetadataTruthValue.TRUE
