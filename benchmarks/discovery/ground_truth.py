"""Human-owned ground-truth loading and validation.

The evaluated retrieval engine must never populate this file automatically.
Candidate exports are deliberately a separate artifact with an ``unreviewed``
state; only a human-reviewed definition is accepted here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

GROUND_TRUTH_STATES = frozenset({"relevant", "not_relevant", "uncertain"})
COMPONENT_TYPES = frozenset(
    {
        "email_thread",
        "reply_to_chain",
        "references_chain",
        "message_attachments",
        "document_versions",
        "standalone_document",
        "explicit_document_group",
        "other",
    }
)


def load_ground_truth(path: str | Path) -> dict[str, Any]:
    """Load and validate one versioned YAML benchmark definition."""
    with Path(path).open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError("ground truth must be a YAML object")
    validate_ground_truth(value)
    return value


def validate_ground_truth(value: dict[str, Any]) -> None:
    """Fail on ambiguous identities or internally inconsistent components."""
    if value.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    for field in ("benchmark_id", "benchmark_version"):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"{field} is required")
    if value.get("document_metric_unit") != "source_occurrence":
        raise ValueError("document_metric_unit must be source_occurrence")

    queries = value.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries must contain at least one query")
    query_ids: set[str] = set()
    for query in queries:
        if not isinstance(query, dict):
            raise ValueError("each query must be an object")
        query_id = str(query.get("query_id") or "").strip()
        text = str(query.get("text") or "").strip()
        if not query_id or not text:
            raise ValueError("each query needs query_id and text")
        if query_id in query_ids:
            raise ValueError(f"duplicate query_id: {query_id}")
        query_ids.add(query_id)

    documents = value.get("documents", [])
    components = value.get("components", [])
    if not isinstance(documents, list) or not isinstance(components, list):
        raise ValueError("documents and components must be lists")

    occurrence_ids: set[str] = set()
    document_by_occurrence: dict[str, dict[str, Any]] = {}
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("each document must be an object")
        occurrence_id = str(document.get("occurrence_id") or "").strip()
        if not occurrence_id:
            raise ValueError("every document needs an explicit occurrence_id")
        if occurrence_id in occurrence_ids:
            raise ValueError(f"duplicate occurrence_id: {occurrence_id}")
        occurrence_ids.add(occurrence_id)
        document_by_occurrence[occurrence_id] = document
        if document.get("state") not in GROUND_TRUTH_STATES:
            raise ValueError(f"invalid state for {occurrence_id}")
        if not str(document.get("document_id") or "").strip():
            raise ValueError(f"document_id is required for {occurrence_id}")
        if not str(document.get("source_entity_id") or "").strip():
            raise ValueError(f"source_entity_id is required for {occurrence_id}")

    component_ids: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("each component must be an object")
        component_id = str(component.get("component_id") or "").strip()
        if not component_id or component_id in component_ids:
            raise ValueError(f"invalid or duplicate component_id: {component_id}")
        component_ids.add(component_id)
        if component.get("state") not in GROUND_TRUTH_STATES:
            raise ValueError(f"invalid state for component {component_id}")
        if component.get("type") not in COMPONENT_TYPES:
            raise ValueError(f"invalid type for component {component_id}")
        members = component.get("required_occurrence_ids")
        if not isinstance(members, list) or not members:
            raise ValueError(f"component {component_id} needs required_occurrence_ids")
        if len(set(members)) != len(members):
            raise ValueError(f"component {component_id} has duplicate members")
        unknown = set(members) - occurrence_ids
        if unknown:
            raise ValueError(f"component {component_id} has unknown members: {sorted(unknown)}")

    for occurrence_id, document in document_by_occurrence.items():
        state = document["state"]
        component_id = document.get("component_id")
        if state == "relevant":
            if component_id not in component_ids:
                raise ValueError(f"relevant document {occurrence_id} needs a valid component_id")
            component = next(item for item in components if item["component_id"] == component_id)
            if occurrence_id not in component["required_occurrence_ids"]:
                raise ValueError(
                    f"relevant document {occurrence_id} is missing from component {component_id}"
                )
