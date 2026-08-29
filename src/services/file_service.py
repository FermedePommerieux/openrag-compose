"""Server-side document listing and filename search.

The documents index stores one OpenSearch hit per chunk.  A completed
ingestion gives every document exactly one representative chunk with
``chunk_index == 0`` and copies document-level metadata onto that chunk.  The
Knowledge browser queries only those representatives: OpenSearch therefore
returns exactly the requested UI page and ``track_total_hits`` provides the
exact document count without a 10,000-bucket aggregation.

Pages use an opaque ``search_after`` cursor.  This avoids OpenSearch's
``index.max_result_window`` limit while retaining stable next/previous
navigation over collections of any size.
"""

import base64
import json
from typing import Any

from config.settings import get_index_name
from utils.logging_config import get_logger

logger = get_logger(__name__)

# Fetch the visible page plus five bounded look-ahead pages.  The frontend
# caches those pages, which keeps ordinary next-page navigation instant without
# returning an arbitrary 10,000-document window.  At the largest supported UI
# page size this remains bounded to 6,000 lightweight representative chunks.
FILE_PAGE_LOOKAHEAD = 5

_FILE_SOURCE_FIELDS = [
    "document_id",
    "filename",
    "mimetype",
    "file_size",
    "source_url",
    "source_provenance",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_entity_alternate_ids",
    "source_relation_target_ids",
    "source_relation_roles",
    "source_relative_path",
    "source_path_ancestors",
    "owner",
    "owner_name",
    "owner_email",
    "connector_type",
    "embedding_model",
    "embedding_dimensions",
    "indexed_time",
    "document_chunk_count",
    "allowed_users",
    "allowed_groups",
    "allowed_principal_labels",
]

_SORT_FIELDS = {
    "filename": "filename",
    "file_size": "file_size",
    "mimetype": "mimetype",
    "indexed_time": "indexed_time",
    "connector_type": "connector_type",
    "chunk_count": "document_chunk_count",
    "owner": "owner",
}


class FileService:
    """Provides file-level views over the chunk-based OpenSearch index."""

    def __init__(self, session_manager=None):
        self.session_manager = session_manager

    async def list_files(
        self,
        user_id: str,
        jwt_token: str = None,
        page: int = 1,
        page_size: int = 25,
        sort_by: str = "filename",
        sort_order: str = "asc",
        connector_type: str | None = None,
        mimetype: str | None = None,
        owner: str | None = None,
        search: str | None = None,
        data_sources: list[str] | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """
        List ingested files with server-side pagination, filtering, and sorting.

        Query only the representative chunk for each document and return one
        bounded page.  ``cursor`` is the prior page's opaque ``search_after``
        value; page 1 intentionally has no cursor.
        """
        opensearch_client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)

        query = self._build_filter_query(
            user_id,
            connector_type,
            mimetype,
            owner,
            search,
            data_sources,
        )
        representative_filters = query["bool"].setdefault("filter", [])
        representative_filters.append({"term": {"chunk_index": 0}})

        resolved_sort_field = _SORT_FIELDS.get(sort_by, "filename")
        resolved_sort_order = "desc" if sort_order.lower() == "desc" else "asc"
        body: dict[str, Any] = {
            "size": page_size * (FILE_PAGE_LOOKAHEAD + 1),
            "track_total_hits": True,
            "query": query,
            "_source": _FILE_SOURCE_FIELDS,
            "sort": [
                {
                    resolved_sort_field: {
                        "order": resolved_sort_order,
                        "missing": "_last",
                    }
                },
                {"document_id": {"order": "asc"}},
            ],
        }
        if cursor:
            body["search_after"] = self._decode_cursor(cursor)
        elif page > 1:
            # Backward compatibility for API clients that have not adopted
            # cursors yet.  The Knowledge UI always uses search_after, so its
            # navigation is not constrained by max_result_window.
            offset = (page - 1) * page_size
            if offset + page_size > 10_000:
                raise ValueError("A cursor is required beyond OpenSearch's 10,000-result window")
            # Legacy offset clients receive only the requested page. They do
            # not provide the stable boundary required for safe look-ahead.
            body["size"] = page_size
            body["from"] = offset

        try:
            result = await opensearch_client.search(
                index=get_index_name(),
                body=body,
            )
        except Exception as e:
            logger.error("Failed to list files", error=str(e))
            # An auth failure (OpenSearch rejected the credential) must not be
            # masked as an empty result — surface it so the route returns 401.
            from utils.opensearch_utils import is_opensearch_auth_error

            if is_opensearch_auth_error(e):
                raise
            return {
                "files": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "next_cursor": None,
                "prefetched_pages": [],
            }

        hits_block = result.get("hits", {})
        hits = hits_block.get("hits", [])
        total_value = hits_block.get("total", 0)
        total = total_value.get("value", 0) if isinstance(total_value, dict) else total_value
        current_hits = hits[:page_size]
        files = [self._parse_representative_hit(hit) for hit in current_hits]
        has_next_page = page * page_size < int(total or 0)
        next_cursor = None
        if has_next_page and current_hits:
            next_cursor = self._cursor_from_hit(current_hits[-1])

        prefetched_pages = []
        for lookahead in range(1, FILE_PAGE_LOOKAHEAD + 1):
            start = lookahead * page_size
            page_hits = hits[start : start + page_size]
            if not page_hits:
                break
            prefetched_page_number = page + lookahead
            prefetched_page_cursor = self._cursor_from_hit(hits[start - 1])
            prefetched_next_cursor = None
            if prefetched_page_number * page_size < int(total or 0):
                prefetched_next_cursor = self._cursor_from_hit(page_hits[-1])
            prefetched_pages.append(
                {
                    "page": prefetched_page_number,
                    "cursor": prefetched_page_cursor,
                    "files": [self._parse_representative_hit(hit) for hit in page_hits],
                    "next_cursor": prefetched_next_cursor,
                }
            )

        return {
            "files": files,
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
            "next_cursor": next_cursor,
            "prefetched_pages": prefetched_pages,
        }

    async def search_files(
        self,
        user_id: str,
        jwt_token: str = None,
        query: str = "",
        page: int = 1,
        page_size: int = 25,
        connector_type: str | None = None,
        mimetype: str | None = None,
        owner: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """
        Search files by name with fuzzy/prefix matching.

        Uses wildcard and prefix queries on the representative filename.
        """
        return await self.list_files(
            user_id=user_id,
            jwt_token=jwt_token,
            page=page,
            page_size=page_size,
            sort_by="filename",
            sort_order="asc",
            connector_type=connector_type,
            mimetype=mimetype,
            owner=owner,
            search=query,
            cursor=cursor,
        )

    def _build_filter_query(
        self,
        user_id: str,
        connector_type: str | None = None,
        mimetype: str | None = None,
        owner: str | None = None,
        search: str | None = None,
        data_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build the bool query with optional filters + filename search."""
        must: list[dict[str, Any]] = []
        filter_clauses: list[dict[str, Any]] = []

        if connector_type:
            filter_clauses.append({"term": {"connector_type": connector_type}})
        if mimetype:
            filter_clauses.append({"term": {"mimetype": mimetype}})
        if owner:
            filter_clauses.append({"term": {"owner": owner}})

        # `data_sources` is the exact, batch-friendly counterpart to the
        # human-facing fuzzy `search` filter. Connectors use it to prove that
        # the precise filenames they submitted are durably present after an
        # asynchronous ingestion task completes.
        exact_filenames = list(dict.fromkeys(data_sources or []))
        if exact_filenames:
            filter_clauses.append({"terms": {"filename": exact_filenames}})

        if search:
            # Combine wildcard (partial), prefix, and fuzzy for flexible matching
            must.append(
                {
                    "bool": {
                        "should": [
                            {"wildcard": {"filename": {"value": f"*{search.lower()}*"}}},
                            {"prefix": {"filename": search.lower()}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )

        query: dict[str, Any] = {"bool": {"filter": filter_clauses}}
        if must:
            query["bool"]["must"] = must

        return query

    @staticmethod
    def _parse_representative_hit(hit: dict[str, Any]) -> dict[str, Any]:
        """Convert one document's representative chunk into a file row."""
        source = hit.get("_source", {})
        return {
            "filename": source.get("filename", ""),
            "document_id": source.get("document_id", ""),
            "mimetype": source.get("mimetype", ""),
            "file_size": source.get("file_size", 0),
            "source_url": source.get("source_url", ""),
            "source_provenance": source.get("source_provenance"),
            "source_entity_id": source.get("source_entity_id"),
            "source_entity_type": source.get("source_entity_type"),
            "source_entity_system": source.get("source_entity_system"),
            "source_entity_alternate_ids": source.get("source_entity_alternate_ids", []),
            "source_relation_target_ids": source.get("source_relation_target_ids", []),
            "source_relation_roles": source.get("source_relation_roles", []),
            "source_relative_path": source.get("source_relative_path"),
            "source_path_ancestors": source.get("source_path_ancestors", []),
            "owner": source.get("owner", ""),
            "owner_name": source.get("owner_name", ""),
            "owner_email": source.get("owner_email", ""),
            "connector_type": source.get("connector_type", ""),
            "embedding_model": source.get("embedding_model", ""),
            "embedding_dimensions": source.get("embedding_dimensions"),
            "indexed_time": source.get("indexed_time", ""),
            "chunk_count": source.get("document_chunk_count", 0),
            "allowed_users": source.get("allowed_users", []),
            "allowed_groups": source.get("allowed_groups", []),
            "allowed_principal_labels": source.get("allowed_principal_labels", []),
        }

    @staticmethod
    def _encode_cursor(sort_values: list[Any]) -> str:
        payload = json.dumps(sort_values, ensure_ascii=False, separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @classmethod
    def _cursor_from_hit(cls, hit: dict[str, Any]) -> str:
        sort_values = hit.get("sort")
        if not isinstance(sort_values, list):
            raise RuntimeError("OpenSearch omitted sort values required for pagination")
        return cls._encode_cursor(sort_values)

    @staticmethod
    def _decode_cursor(cursor: str) -> list[Any]:
        try:
            padding = "=" * (-len(cursor) % 4)
            decoded = base64.urlsafe_b64decode((cursor + padding).encode()).decode()
            sort_values = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid file pagination cursor") from exc
        if not isinstance(sort_values, list) or len(sort_values) != 2:
            raise ValueError("Invalid file pagination cursor")
        return sort_values
