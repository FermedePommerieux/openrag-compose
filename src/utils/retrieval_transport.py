"""Bounded transport projections for retrieval consumers."""

from typing import Any

LANGFLOW_DOCUMENT_FIELDS = (
    "document_id",
    "filename",
    "mimetype",
    "connector_type",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_entity_alternate_ids",
    "source_relation_target_ids",
    "source_relation_roles",
    "source_relative_path",
    "source_path_ancestors",
    "generated_at_time",
    "complete",
    "status_code",
    "error",
)


def _present_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: value[field]
        for field in fields
        if field in value and value[field] not in (None, "", [], {})
    }


def project_scope_exhaustive_for_langflow(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove scope-sized evidence before the backend-to-Langflow boundary.

    Scope certification still consumes every verified chunk inside the backend.
    Langflow receives only the already-bounded model evidence, one compact row
    per document occurrence and the backend-authored coverage certificate.

    The exhaustive leaves are intentionally not persisted. Cited leaves can be
    resolved later by immutable ``chunk_id`` through the caller's DLS-scoped
    OpenSearch client.
    """
    model_results = payload.get("model_results", [])
    projected_results = [dict(item) for item in model_results if isinstance(item, dict)]
    documents = payload.get("documents", [])
    manifest = [
        _present_fields(item, LANGFLOW_DOCUMENT_FIELDS)
        for item in documents
        if isinstance(item, dict)
    ]

    compact: dict[str, Any] = {
        "results": projected_results,
        "total": payload.get("total", len(projected_results)),
        "documents": manifest,
        "coverage": dict(payload.get("coverage", {})),
        "transport": {
            "profile": "langflow",
            "scope_evidence_omitted": True,
            "source_resolution": "dls_chunk_id",
        },
    }
    for field in ("error", "warning", "retrieval_strategy"):
        if field in payload and payload[field] not in (None, ""):
            compact[field] = payload[field]
    return compact
