"""Strict, queryable source provenance based on W3C PROV-O.

``source_url`` answers "where can I inspect the retained source?".  The
models in this module answer the different questions "what source entity is
this?" and "how is it related to other source entities?".  Keeping those
concepts separate prevents a mutable URL from becoming a document identity.

OpenRAG deliberately accepts a small PROV-O profile instead of arbitrary
JSON-LD.  A bounded, typed profile can be validated at the untrusted ingestion
boundary, indexed without dynamic-mapping surprises, and carried through the
Langflow callback token without giving callers control over index structure.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

PROV_NAMESPACE = "http://www.w3.org/ns/prov#"
SOURCE_PROVENANCE_SCHEMA_VERSION = "1.0"
MAX_SOURCE_RELATIONS = 256


class SourceRelationRole(StrEnum):
    """OpenRAG relation roles with an explicit PROV-O predicate."""

    ATTACHMENT_OF = "attachment_of"
    MEMBER_OF = "member_of"
    REPLY_TO = "reply_to"
    REFERENCES = "references"
    CONTAINED_IN = "contained_in"
    OCCURRENCE_OF = "occurrence_of"
    DERIVED_FROM = "derived_from"
    PRIMARY_SOURCE = "primary_source"


ROLE_TO_PROV_PREDICATE: dict[SourceRelationRole, str] = {
    # An email and an archive are collections whose MIME parts/archive entries
    # are entities.  PROV-O collection membership is therefore more precise
    # than treating physical containment as generic derivation.
    SourceRelationRole.ATTACHMENT_OF: f"{PROV_NAMESPACE}wasMemberOf",
    SourceRelationRole.MEMBER_OF: f"{PROV_NAMESPACE}wasMemberOf",
    SourceRelationRole.CONTAINED_IN: f"{PROV_NAMESPACE}wasMemberOf",
    # A reply is influenced by the referenced message, but is not necessarily
    # a textual derivation of it (quoted text may be absent).
    SourceRelationRole.REPLY_TO: f"{PROV_NAMESPACE}wasInfluencedBy",
    SourceRelationRole.REFERENCES: f"{PROV_NAMESPACE}wasInfluencedBy",
    SourceRelationRole.OCCURRENCE_OF: f"{PROV_NAMESPACE}specializationOf",
    SourceRelationRole.DERIVED_FROM: f"{PROV_NAMESPACE}wasDerivedFrom",
    SourceRelationRole.PRIMARY_SOURCE: f"{PROV_NAMESPACE}hadPrimarySource",
}


def _validate_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > 1024:
        raise ValueError(f"{field_name} must not exceed 1024 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} must not contain control characters")
    return normalized


class SourceEntity(BaseModel):
    """A stable PROV Entity identity, independent from its access URL."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    source_system: str | None = None
    label: str | None = Field(default=None, max_length=512)
    alternate_ids: list[str] = Field(default_factory=list, max_length=64)
    generated_at_time: datetime | None = None

    @field_validator("id", "type", "source_system")
    @classmethod
    def validate_identifiers(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator("alternate_ids")
    @classmethod
    def validate_alternate_ids(cls, values: list[str]) -> list[str]:
        normalized = [_validate_identifier(value, field_name="alternate_ids") for value in values]
        # Preserve caller order because RFC 5322 References order conveys the
        # ancestry path, while removing exact duplicates deterministically.
        return list(dict.fromkeys(normalized))


class SourceRelation(BaseModel):
    """A directed, qualified relation from the current entity to a target."""

    model_config = ConfigDict(extra="forbid")

    role: SourceRelationRole
    target: SourceEntity
    prov_predicate: str | None = None

    @model_validator(mode="after")
    def validate_prov_predicate(self) -> SourceRelation:
        expected = ROLE_TO_PROV_PREDICATE[self.role]
        if self.prov_predicate is not None and self.prov_predicate != expected:
            raise ValueError(f"prov_predicate for role {self.role.value!r} must be {expected!r}")
        # Persist the resolved full URI so exported records are self-describing
        # and do not depend on a JSON-LD prefix context.
        self.prov_predicate = expected
        return self


class SourceProvenance(BaseModel):
    """Versioned provenance envelope attached to one ingested document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    entity: SourceEntity
    relations: list[SourceRelation] = Field(
        default_factory=list,
        max_length=MAX_SOURCE_RELATIONS,
    )

    @field_validator("relations")
    @classmethod
    def reject_duplicate_relations(cls, relations: list[SourceRelation]) -> list[SourceRelation]:
        seen: set[tuple[str, str]] = set()
        for relation in relations:
            identity = (relation.role.value, relation.target.id)
            if identity in seen:
                raise ValueError(
                    "source provenance contains a duplicate relation "
                    f"{relation.role.value!r} -> {relation.target.id!r}"
                )
            seen.add(identity)
        return relations

    def index_fields(self) -> dict[str, object]:
        """Return the canonical object plus safe denormalized query fields.

        OpenSearch has no relational joins.  The flattened keyword arrays make
        reverse traversal efficient, while ``source_provenance`` preserves the
        target/role pairing needed for proof and export.
        """
        return {
            "source_provenance": self.model_dump(mode="json", exclude_none=True),
            "source_entity_id": self.entity.id,
            "source_entity_type": self.entity.type,
            "source_entity_system": self.entity.source_system or "",
            "source_entity_alternate_ids": list(self.entity.alternate_ids),
            "source_relation_target_ids": [relation.target.id for relation in self.relations],
            # Full PROV-O predicate URIs are the canonical retrieval field.
            # ``source_relation_roles`` remains a bounded OpenRAG qualifier
            # used for policies that PROV-O alone cannot express, such as
            # distinguishing an attachment collection from a broad archive.
            "source_relation_predicates": [
                relation.prov_predicate or ROLE_TO_PROV_PREDICATE[relation.role]
                for relation in self.relations
            ],
            "source_relation_roles": [relation.role.value for relation in self.relations],
        }


def parse_source_provenance(value: object) -> SourceProvenance | None:
    """Validate an optional provenance object at a service boundary."""
    if value is None:
        return None
    if isinstance(value, SourceProvenance):
        return value
    return SourceProvenance.model_validate(value)


def source_provenance_mapping() -> dict[str, Any]:
    """Return the explicit OpenSearch mapping for the bounded PROV-O profile."""
    entity_properties: dict[str, object] = {
        "id": {"type": "keyword"},
        "type": {"type": "keyword"},
        "source_system": {"type": "keyword"},
        "label": {"type": "keyword", "ignore_above": 512},
        "alternate_ids": {"type": "keyword", "ignore_above": 1024},
        "generated_at_time": {"type": "date"},
    }
    return {
        "properties": {
            "schema_version": {"type": "keyword"},
            "entity": {"properties": entity_properties},
            # Nested mapping preserves the role/target pairing. Flattened
            # top-level keyword arrays serve reverse traversal without a
            # costly nested query when only one dimension is needed.
            "relations": {
                "type": "nested",
                "properties": {
                    "role": {"type": "keyword"},
                    "prov_predicate": {"type": "keyword"},
                    "target": {"properties": entity_properties},
                },
            },
        }
    }
