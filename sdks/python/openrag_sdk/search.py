"""OpenRAG SDK search client."""

from typing import TYPE_CHECKING, Any

from .models import SearchFilters, SearchResponse, SearchResult

if TYPE_CHECKING:
    from .client import OpenRAGClient


class SearchClient:
    """Client for search operations."""

    def __init__(self, client: "OpenRAGClient"):
        self._client = client

    async def query(
        self,
        query: str,
        *,
        filters: SearchFilters | dict[str, Any] | None = None,
        limit: int = 10,
        score_threshold: float = 0,
        filter_id: str | None = None,
        evidence_mode: str = "focused",
        document_id: str | None = None,
        cursor: str = "",
        batch_size: int = 20,
    ) -> SearchResponse:
        """
        Search for documents or read one immutable document snapshot exhaustively.

        Args:
            query: The search query text.
            filters: Optional filters (data_sources, document_types).
            limit: Maximum number of results (default 10).
            score_threshold: Minimum score threshold (default 0).
            filter_id: Optional knowledge filter ID to apply.
            evidence_mode: ``focused`` discovery, ``exhaustive`` one-document
                reading, or ``scope_exhaustive`` dossier investigation.
            document_id: Required for exhaustive reading.
            cursor: Opaque continuation cursor from the previous coverage object.
            batch_size: Exhaustive page size, between 1 and 50.

        Returns:
            SearchResponse containing focused matches or an exhaustive page and its
            machine-verifiable coverage certificate. In exhaustive mode, keep
            calling with ``coverage.next_cursor`` until ``coverage.complete``.
        """
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "score_threshold": score_threshold,
            "evidence_mode": evidence_mode,
            "batch_size": batch_size,
        }

        if filters:
            if isinstance(filters, SearchFilters):
                body["filters"] = filters.model_dump(exclude_none=True)
            else:
                body["filters"] = filters

        if filter_id:
            body["filter_id"] = filter_id
        if document_id:
            body["document_id"] = document_id
        if cursor:
            body["cursor"] = cursor

        response = await self._client._request(
            "POST",
            "/api/v1/search",
            json=body,
        )

        data = response.json()
        return SearchResponse(
            results=[SearchResult(**r) for r in data.get("results", [])],
            coverage=data.get("coverage"),
            error=data.get("error"),
            documents=data.get("documents", []),
            evidence_batches=data.get("evidence_batches", []),
            graph=data.get("graph"),
        )
