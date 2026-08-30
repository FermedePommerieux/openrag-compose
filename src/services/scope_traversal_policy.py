"""Versioned documentary-scope policy for the PROV-O source graph.

The provenance graph records both documentary relationships and ingestion
infrastructure.  This module is the single, pure decision boundary that says
which typed relations may expand ``scope_exhaustive``.  Unknown combinations
fail closed for certification; they are never silently traversed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

SCOPE_POLICY_ID = "documentary-prov-o"
SCOPE_POLICY_VERSION = 1
RFC5322_MESSAGE_ID_URN_PREFIX = "urn:openrag:rfc5322:message-id:"


class ScopeRelationSemantics(StrEnum):
    """Stable semantic classes exposed by scope coverage accounting."""

    SCOPE_DEFINING = "scope-defining"
    CONTEXTUAL = "contextual"
    IDENTITY_ONLY = "identity-only"
    INFRASTRUCTURE = "infrastructure"
    UNCLASSIFIED = "unclassified"


Transitivity = bool | Literal["controlled"]


@dataclass(frozen=True)
class ScopeTraversalDecision:
    """Pure decision for one fully typed directed relation."""

    follow_forward: bool
    follow_reverse: bool
    transitive: Transitivity
    semantics: ScopeRelationSemantics
    certifiable: bool

    def follows(self, direction: Literal["forward", "reverse"]) -> bool:
        return self.follow_forward if direction == "forward" else self.follow_reverse


@dataclass(frozen=True)
class ScopeTraversalRule:
    role: str
    source_type: str
    target_type: str
    decision: ScopeTraversalDecision


_SCOPE = ScopeTraversalDecision(
    follow_forward=True,
    follow_reverse=True,
    transitive=True,
    semantics=ScopeRelationSemantics.SCOPE_DEFINING,
    certifiable=True,
)
_ATTACHMENT_SCOPE = ScopeTraversalDecision(
    follow_forward=True,
    follow_reverse=True,
    transitive="controlled",
    semantics=ScopeRelationSemantics.SCOPE_DEFINING,
    certifiable=True,
)
_CONTEXT = ScopeTraversalDecision(
    follow_forward=False,
    follow_reverse=False,
    transitive=False,
    semantics=ScopeRelationSemantics.CONTEXTUAL,
    certifiable=True,
)
_INFRASTRUCTURE = ScopeTraversalDecision(
    follow_forward=False,
    follow_reverse=False,
    transitive=False,
    semantics=ScopeRelationSemantics.INFRASTRUCTURE,
    certifiable=True,
)
_UNCLASSIFIED = ScopeTraversalDecision(
    follow_forward=False,
    follow_reverse=False,
    transitive=False,
    semantics=ScopeRelationSemantics.UNCLASSIFIED,
    certifiable=False,
)


def _rules() -> tuple[ScopeTraversalRule, ...]:
    rules: list[ScopeTraversalRule] = [
        ScopeTraversalRule("attachment_of", "email_attachment", "email_message", _ATTACHMENT_SCOPE),
        ScopeTraversalRule("member_of", "file", "directory_collection", _INFRASTRUCTURE),
    ]
    rules.extend(
        ScopeTraversalRule("member_of", source_type, "email_thread", _SCOPE)
        for source_type in ("email_message", "email_attachment")
    )
    rules.extend(
        ScopeTraversalRule("contained_in", source_type, "email_archive", _CONTEXT)
        for source_type in ("email_message", "email_attachment")
    )
    rules.extend(
        ScopeTraversalRule(role, "email_message", target_type, _SCOPE)
        for role in ("reply_to", "references")
        for target_type in ("email_message", "email_message_identifier")
    )
    return tuple(rules)


class ScopeTraversalPolicy:
    """Deterministic, LLM-independent documentary traversal policy v1."""

    policy_id = SCOPE_POLICY_ID
    version = SCOPE_POLICY_VERSION

    def __init__(self) -> None:
        self._rules = _rules()
        self._decisions = {
            (rule.role, rule.source_type, rule.target_type): rule.decision for rule in self._rules
        }

    @property
    def rules(self) -> tuple[ScopeTraversalRule, ...]:
        return self._rules

    def classify(
        self,
        *,
        role: object,
        source_type: object,
        target_type: object,
    ) -> ScopeTraversalDecision:
        """Classify one exact role/type triple, failing closed by default."""
        key: tuple[str, str, str] = (
            role.strip() if isinstance(role, str) else "",
            source_type.strip() if isinstance(source_type, str) else "",
            target_type.strip() if isinstance(target_type, str) else "",
        )
        return self._decisions.get(key, _UNCLASSIFIED)

    def reverse_rules_for_target(self, target_type: str) -> tuple[ScopeTraversalRule, ...]:
        """Return only rules allowed to issue a reverse query for this target type."""
        return tuple(
            rule
            for rule in self._rules
            if rule.target_type == target_type and rule.decision.follow_reverse
        )

    def allows_shared_alternate_identity(
        self,
        identifier: str,
        records: tuple[dict[str, Any], ...],
    ) -> bool:
        """Allow verified duplicate RFC 5322 occurrences without merging owners.

        An RFC Message-ID can identify one message retained in several source
        containers.  Such occurrences remain separate primary entities.  A
        shared alias is accepted only when message type, timestamp and subject
        agree and every owner belongs to a distinct explicit source container.
        Missing or conflicting evidence stays ambiguous.
        """
        if not identifier.startswith(RFC5322_MESSAGE_ID_URN_PREFIX) or len(records) < 2:
            return False
        primary_ids = {str(record.get("source_entity_id") or "") for record in records}
        if "" in primary_ids or len(primary_ids) != len(records):
            return False
        if any(record.get("source_entity_type") != "email_message" for record in records):
            return False
        if any(
            identifier not in record.get("source_entity_alternate_ids", []) for record in records
        ):
            return False

        timestamps = {str(record.get("generated_at_time") or "") for record in records}
        labels = {
            re.sub(r"\s+", " ", str(record.get("source_entity_label") or "").strip()).casefold()
            for record in records
        }
        if "" in timestamps or len(timestamps) != 1 or "" in labels or len(labels) != 1:
            return False

        containers: list[str] = []
        for record in records:
            provenance = record.get("source_provenance")
            relations = provenance.get("relations", []) if isinstance(provenance, dict) else []
            record_containers = {
                str(target.get("id") or "")
                for relation in relations
                if isinstance(relation, dict)
                and relation.get("role") == "contained_in"
                and isinstance((target := relation.get("target")), dict)
                and target.get("type") == "email_archive"
                and target.get("id")
            }
            if len(record_containers) != 1:
                return False
            containers.extend(record_containers)
        return len(set(containers)) == len(records)


DEFAULT_SCOPE_TRAVERSAL_POLICY = ScopeTraversalPolicy()
