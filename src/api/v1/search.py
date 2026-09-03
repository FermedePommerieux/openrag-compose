"""
Public API v1 Search endpoint.

Provides semantic search functionality.
Uses API key authentication.
"""

from typing import Any, Literal

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from api.v1._filter_resolution import merge_filter_overrides, resolve_filter_id
from auth_context import set_auth_context
from dependencies import (
    get_knowledge_filter_service,
    get_search_service,
    require_api_key_permission,
)
from models.metadata_filter import MetadataFilter
from session_manager import User
from utils.logging_config import get_logger
from utils.opensearch_utils import DISK_SPACE_ERROR_MESSAGE, OpenSearchDiskSpaceError

logger = get_logger(__name__)


class SearchV1Body(BaseModel):
    query: str | None = None
    free_text: str | None = None
    metadata_filter: MetadataFilter | None = None
    filters: dict[str, Any] | None = None
    limit: int = 10
    score_threshold: float = 0
    filter_id: str | None = None
    evidence_mode: Literal["focused", "exhaustive", "scope_exhaustive"] = "focused"
    document_id: str | None = None
    cursor: str = ""
    batch_size: int = Field(default=20, ge=1, le=50)
    multi_query_discovery: bool = False
    multi_query_max_queries: int = Field(default=4, ge=1, le=4)
    multi_query_concurrency: int = Field(default=2, ge=1, le=4)

    @model_validator(mode="after")
    def validate_discovery_input(self) -> "SearchV1Body":
        if self.query is not None and self.free_text is not None and self.query != self.free_text:
            raise ValueError("query and free_text cannot disagree")
        if not self.resolved_free_text.strip() and self.evidence_mode != "exhaustive":
            raise ValueError("free_text is required for retrieval")
        return self

    @property
    def resolved_free_text(self) -> str:
        return self.free_text if self.free_text is not None else self.query or ""


async def search_endpoint(
    body: SearchV1Body,
    search_service=Depends(get_search_service),
    user: User = Depends(require_api_key_permission("search:use")),
    knowledge_filter_service=Depends(get_knowledge_filter_service),
):
    """Perform semantic search on documents. POST /v1/search"""
    query = body.resolved_free_text.strip()
    if body.evidence_mode == "exhaustive" and not (body.document_id or "").strip():
        return JSONResponse(
            {"error": "document_id is required for exhaustive retrieval"},
            status_code=400,
        )

    # API-key requests can arrive without a JWT. Set the auth context before
    # resolving filters so search_tool() can still identify the caller.
    set_auth_context(user.user_id, user.jwt_token)

    resolved_filters = body.filters
    resolved_limit = body.limit
    resolved_score_threshold = body.score_threshold
    if body.filter_id:
        resolved = await resolve_filter_id(
            body.filter_id,
            knowledge_filter_service,
            user_id=user.user_id,
            jwt_token=user.jwt_token,
        )
        resolved_filters, resolved_limit, resolved_score_threshold = merge_filter_overrides(
            resolved, body
        )

    logger.debug(
        "Public API search request",
        user_id=user.user_id,
        query=query,
        filters=resolved_filters,
        limit=resolved_limit,
        score_threshold=resolved_score_threshold,
        filter_id=body.filter_id,
        metadata_filter_sha256=(
            body.metadata_filter.calculate_sha256() if body.metadata_filter else None
        ),
    )

    try:
        result = await search_service.search(
            query,
            user_id=user.user_id,
            jwt_token=user.jwt_token,
            filters=resolved_filters or {},
            limit=resolved_limit,
            score_threshold=resolved_score_threshold,
            evidence_mode=body.evidence_mode,
            document_id=body.document_id,
            cursor=body.cursor,
            batch_size=min(50, max(1, body.batch_size)),
            multi_query_discovery=body.multi_query_discovery,
            multi_query_max_queries=body.multi_query_max_queries,
            multi_query_concurrency=body.multi_query_concurrency,
            metadata_filter=body.metadata_filter,
        )

        if body.evidence_mode in {"exhaustive", "scope_exhaustive"}:
            return JSONResponse(result)

        results = [
            {
                "filename": item.get("filename"),
                "document_id": item.get("document_id"),
                "chunk_id": item.get("chunk_id"),
                "chunk_index": item.get("chunk_index"),
                "chunking_strategy": item.get("chunking_strategy"),
                "text": item.get("text"),
                "score": item.get("score"),
                "page": item.get("page"),
                "mimetype": item.get("mimetype"),
                "source_url": item.get("source_url"),
                "connector_file_id": item.get("connector_file_id"),
                "source_provenance": item.get("source_provenance"),
                "source_entity_id": item.get("source_entity_id"),
                "source_entity_type": item.get("source_entity_type"),
                "source_entity_system": item.get("source_entity_system"),
                "source_entity_alternate_ids": item.get("source_entity_alternate_ids", []),
                "source_relation_target_ids": item.get("source_relation_target_ids", []),
                "source_relation_roles": item.get("source_relation_roles", []),
                "source_relative_path": item.get("source_relative_path"),
                "source_path_ancestors": item.get("source_path_ancestors", []),
                "matched_queries": item.get("matched_queries", []),
                "matched_lanes": item.get("matched_lanes", []),
                "best_rank_per_query": item.get("best_rank_per_query", {}),
                "query_contributions": item.get("query_contributions", []),
                "fusion_score": item.get("fusion_score"),
            }
            for item in result.get("results", [])
        ]

        response: dict[str, Any] = {"results": results}
        if isinstance(result.get("discovery"), dict):
            response["discovery"] = result["discovery"]
        if isinstance(result.get("warnings"), list):
            response["warnings"] = result["warnings"]
        if isinstance(result.get("metadata_filter"), dict):
            response["metadata_filter"] = result["metadata_filter"]
        return JSONResponse(response)

    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OpenSearchDiskSpaceError as e:
        logger.error("Search blocked by disk space constraint", error=str(e), user_id=user.user_id)
        return JSONResponse({"error": DISK_SPACE_ERROR_MESSAGE}, status_code=507)
    except Exception as e:
        error_msg = str(e)
        logger.error("Search failed", error=error_msg, user_id=user.user_id)
        if "AuthenticationException" in error_msg or "access denied" in error_msg.lower():
            return JSONResponse({"error": error_msg}, status_code=403)
        else:
            return JSONResponse({"error": error_msg}, status_code=500)
