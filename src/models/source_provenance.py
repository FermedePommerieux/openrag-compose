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

import base64
import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote

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
MAX_SOURCE_RELATIVE_PATH_LENGTH = 4096
OPENARCHIVER_ATTACHMENT_CONTRACT = "openrag.openarchiver-attachment-ingestion"
OPENARCHIVER_ATTACHMENT_CONTRACT_VERSION = 1


def _sha256_document_id(value: str) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(value)).rstrip(b"=").decode("ascii")[:24]


def normalize_source_relative_path(value: str) -> str:
    """Return one portable ingestion-relative path or fail explicitly."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("relative_path must not contain control characters")
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("relative_path must not be empty")
    if normalized.startswith("/") or (
        len(normalized) >= 3 and normalized[0].isalpha() and normalized[1:3] == ":/"
    ):
        raise ValueError("relative_path must not be absolute")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must be a normalized path without traversal")
    return "/".join(parts)


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


class OpenArchiverAttachmentContract(BaseModel):
    """Fail-closed binary identity supplied by the OpenArchiver connector.

    This envelope is transported inside the signed ingestion context, but it
    is indexed separately from public source provenance.  Archive locators and
    binary verification facts must never become retrieval/model metadata.
    """

    model_config = ConfigDict(extra="forbid")

    contract: Literal["openrag.openarchiver-attachment-ingestion"] = (
        "openrag.openarchiver-attachment-ingestion"
    )
    version: Literal[1] = 1
    source_kind: Literal["openarchiver_attachment"] = "openarchiver_attachment"
    source_entity_id: str
    parent_source_entity_id: str
    attachment_id: str
    parent_email_id: str
    parent_archive_source_id: str | None = None
    filename_original: str = Field(max_length=4096)
    mime_type_declared: str | None = Field(default=None, max_length=255)
    mime_type_detected: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str
    document_id: str
    archive_locator: str = Field(max_length=2048)
    connector_version: str = Field(max_length=255)

    @field_validator(
        "source_entity_id",
        "parent_source_entity_id",
        "attachment_id",
        "parent_email_id",
        "document_id",
        "archive_locator",
        "connector_version",
    )
    @classmethod
    def validate_required_identifiers(cls, value: str, info: ValidationInfo) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator("parent_archive_source_id")
    @classmethod
    def validate_optional_parent_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name="parent_archive_source_id")

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != hashlib.sha256().digest_size * 2 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("sha256 must be a complete hexadecimal SHA-256")
        return normalized

    @field_validator("mime_type_declared", "mime_type_detected")
    @classmethod
    def validate_mime_type(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if len(normalized.split("/")) != 2 or any(
            not part
            or any(not (character.isalnum() or character in "!#$&^_.+-") for character in part)
            for part in normalized.split("/")
        ):
            raise ValueError(f"{info.field_name} must be a syntactically valid MIME type")
        return normalized

    @model_validator(mode="after")
    def validate_identity_derivations(self) -> OpenArchiverAttachmentContract:
        expected_entity = "urn:openrag:openarchiver:attachment:" + quote(
            self.attachment_id, safe=""
        )
        parent_parts = ["urn:openrag:openarchiver:email"]
        if self.parent_archive_source_id:
            parent_parts.append(quote(self.parent_archive_source_id, safe=""))
        parent_parts.append(quote(self.parent_email_id, safe=""))
        expected_parent = ":".join(parent_parts)
        if self.source_entity_id != expected_entity:
            raise ValueError("source_entity_id does not match attachment_id")
        if self.parent_source_entity_id != expected_parent:
            raise ValueError("parent_source_entity_id does not match parent_email_id")
        if self.document_id != _sha256_document_id(self.sha256):
            raise ValueError("document_id does not match the verified binary SHA-256")
        return self

    def index_value(self, *, ingested_at: str) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json", exclude_none=True),
            "ingested_at": ingested_at,
        }


class SourceProvenance(BaseModel):
    """Versioned provenance envelope attached to one ingested document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    entity: SourceEntity
    # POSIX path relative to the directory selected as the ingestion point.
    # Absolute host/container paths are deliberately forbidden: provenance
    # must remain portable and must not disclose deployment internals.
    relative_path: str | None = Field(
        default=None,
        max_length=MAX_SOURCE_RELATIVE_PATH_LENGTH,
    )
    relations: list[SourceRelation] = Field(
        default_factory=list,
        max_length=MAX_SOURCE_RELATIONS,
    )
    attachment_contract: OpenArchiverAttachmentContract | None = None

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_source_relative_path(value)

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

    @model_validator(mode="after")
    def validate_attachment_contract(self) -> SourceProvenance:
        contract = self.attachment_contract
        if contract is None:
            return self
        if self.entity.id != contract.source_entity_id:
            raise ValueError("attachment contract source identity differs from provenance entity")
        if self.entity.type != "email_attachment" or self.entity.source_system != "openarchiver":
            raise ValueError("attachment contract requires an OpenArchiver email_attachment entity")
        asserted_parents = {
            relation.target.id
            for relation in self.relations
            if relation.role is SourceRelationRole.ATTACHMENT_OF
        }
        if contract.parent_source_entity_id not in asserted_parents:
            raise ValueError("attachment contract parent is not asserted by attachment_of")
        if any(
            relation.role in {SourceRelationRole.DERIVED_FROM, SourceRelationRole.PRIMARY_SOURCE}
            for relation in self.relations
        ):
            raise ValueError("attachment contract forbids inferred derivation relations")
        return self

    def validate_attachment_binary(
        self,
        *,
        document_id: str,
        size_bytes: int,
    ) -> None:
        contract = self.attachment_contract
        if contract is None:
            return
        if document_id != contract.document_id:
            raise ValueError("attachment binary hash does not match its contract")
        if size_bytes != contract.size_bytes:
            raise ValueError("attachment binary size does not match its contract")

    def index_fields(self, *, indexed_at: str | None = None) -> dict[str, object]:
        """Return the canonical object plus safe denormalized query fields.

        OpenSearch has no relational joins.  The flattened keyword arrays make
        reverse traversal efficient, while ``source_provenance`` preserves the
        target/role pairing needed for proof and export.
        """
        relative_path = self.relative_path or ""
        path_parts = relative_path.split("/") if relative_path else []
        path_ancestors = ["/".join(path_parts[:end]) for end in range(1, len(path_parts))]
        public_provenance = self.model_dump(
            mode="json", exclude_none=True, exclude={"attachment_contract"}
        )
        fields: dict[str, object] = {
            "source_provenance": public_provenance,
            "source_entity_id": self.entity.id,
            "source_entity_type": self.entity.type,
            "source_entity_system": self.entity.source_system or "",
            "source_entity_alternate_ids": list(self.entity.alternate_ids),
            "source_relation_target_ids": [relation.target.id for relation in self.relations],
            "source_relation_roles": [relation.role.value for relation in self.relations],
            "source_relative_path": relative_path,
            "source_path_ancestors": path_ancestors,
        }
        if self.attachment_contract is not None:
            if indexed_at is None:
                raise ValueError("indexed_at is required for an attachment contract")
            fields["source_attachment"] = self.attachment_contract.index_value(
                ingested_at=indexed_at
            )
        return fields


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
            "relative_path": {
                "type": "keyword",
                "ignore_above": MAX_SOURCE_RELATIVE_PATH_LENGTH,
            },
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


def source_attachment_mapping() -> dict[str, Any]:
    """Explicit internal mapping for the future attachment contract."""
    return {
        "properties": {
            "contract": {"type": "keyword"},
            "version": {"type": "integer"},
            "source_kind": {"type": "keyword"},
            "source_entity_id": {"type": "keyword"},
            "parent_source_entity_id": {"type": "keyword"},
            "attachment_id": {"type": "keyword"},
            "parent_email_id": {"type": "keyword"},
            "parent_archive_source_id": {"type": "keyword"},
            "filename_original": {"type": "keyword", "ignore_above": 4096},
            "mime_type_declared": {"type": "keyword"},
            "mime_type_detected": {"type": "keyword"},
            "size_bytes": {"type": "long"},
            "sha256": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "archive_locator": {"type": "keyword", "ignore_above": 2048},
            "ingested_at": {"type": "date"},
            "connector_version": {"type": "keyword"},
        }
    }
