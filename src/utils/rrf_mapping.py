"""Runtime preconditions for deterministic Retrieval v2 rank fusion.

RRF's secondary sort is part of the ranking contract, not a best-effort UI
enhancement.  Keeping the check in one small module makes both startup repair
and query-time enforcement use the same definition of a safe mapping.
"""

from __future__ import annotations

from typing import Any


class RRFMappingError(RuntimeError):
    """Raised when an index cannot provide deterministic RRF ordering."""


def chunk_id_mapping_error(properties: dict[str, Any]) -> str | None:
    """Return an actionable error when ``chunk_id`` cannot be sorted safely.

    Missing values on *documents* are intentionally valid: the search sort
    uses ``missing: _last`` for legacy chunks.  A missing or incompatible
    *mapping* is different, because OpenSearch would reject (or misinterpret)
    the sort for every RRF request.
    """
    mapping = properties.get("chunk_id")
    if not isinstance(mapping, dict):
        return "chunk_id is not mapped; reindex the documents index with a keyword chunk_id field"
    if mapping.get("type") != "keyword":
        return (
            "chunk_id must be mapped as keyword for deterministic RRF; "
            f"found {mapping.get('type')!r}. Reindex the documents index."
        )
    if mapping.get("doc_values") is False:
        return "chunk_id has doc_values disabled; reindex the documents index with sortable keyword doc_values"
    return None


def extract_index_properties(mapping_response: dict[str, Any], index_name: str) -> dict[str, Any]:
    """Extract properties from normal and alias/wildcard OpenSearch responses."""
    if not isinstance(mapping_response, dict):
        return {}
    direct = mapping_response.get(index_name, {})
    candidates = [direct] if isinstance(direct, dict) else []
    candidates.extend(value for value in mapping_response.values() if isinstance(value, dict))
    for candidate in candidates:
        properties = candidate.get("mappings", {}).get("properties", {})
        if isinstance(properties, dict):
            return properties
    return {}


async def require_sortable_chunk_id_mapping(opensearch_client: Any, index_name: str) -> None:
    """Fail closed before executing an RRF query against an unsafe index."""
    try:
        response = await opensearch_client.indices.get_mapping(index=index_name)
    except Exception as exc:
        raise RRFMappingError(
            "Unable to validate the documents index mapping required for RRF; "
            "do not run deterministic retrieval until mapping validation succeeds"
        ) from exc
    error = chunk_id_mapping_error(extract_index_properties(response, index_name))
    if error:
        raise RRFMappingError(f"RRF unavailable: {error}")
