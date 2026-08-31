from typing import Any, Literal

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dependencies import (
    get_search_service,
    get_session_manager,
    require_permission,
)
from session_manager import User
from utils.logging_config import get_logger
from utils.opensearch_utils import DISK_SPACE_ERROR_MESSAGE, OpenSearchDiskSpaceError
from utils.retrieval_transport import project_scope_exhaustive_for_langflow

logger = get_logger(__name__)


class SearchBody(BaseModel):
    query: str
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = 10
    scoreThreshold: float = Field(default=0, alias="scoreThreshold")
    evidenceMode: Literal["focused", "exhaustive", "scope_exhaustive"] = Field(
        default="focused", alias="evidenceMode"
    )
    documentId: str | None = Field(default=None, alias="documentId")
    cursor: str = ""
    batchSize: int = Field(default=20, ge=1, le=50, alias="batchSize")
    groupByDocument: bool = Field(default=False, alias="groupByDocument")
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=100, ge=1, le=1000, alias="pageSize")
    responseProfile: Literal["default", "langflow"] = Field(
        default="default", alias="responseProfile"
    )
    multiQueryDiscovery: bool = Field(default=False, alias="multiQueryDiscovery")
    multiQueryMaxQueries: int = Field(default=4, ge=1, le=4, alias="multiQueryMaxQueries")
    multiQueryConcurrency: int = Field(default=2, ge=1, le=4, alias="multiQueryConcurrency")

    model_config = {"populate_by_name": True}


async def search(
    body: SearchBody,
    search_service=Depends(get_search_service),
    session_manager=Depends(get_session_manager),
    user: User = Depends(require_permission("search:use")),
):
    """Search for documents"""
    try:
        if body.evidenceMode == "exhaustive" and not (body.documentId or "").strip():
            return JSONResponse(
                {"error": "documentId is required for exhaustive retrieval"},
                status_code=400,
            )
        jwt_token = user.jwt_token

        logger.debug(
            "Search API request",
            user_id=user.user_id,
            has_jwt_token=jwt_token is not None,
            query=body.query,
            filters=body.filters,
            limit=body.limit,
            score_threshold=body.scoreThreshold,
            evidence_mode=body.evidenceMode,
            document_id=body.documentId,
            cursor=body.cursor,
            batch_size=body.batchSize,
            group_by_document=body.groupByDocument,
            page=body.page,
            page_size=body.pageSize,
            response_profile=body.responseProfile,
            multi_query_discovery=body.multiQueryDiscovery,
            multi_query_max_queries=body.multiQueryMaxQueries,
            multi_query_concurrency=body.multiQueryConcurrency,
        )

        result = await search_service.search(
            body.query,
            user_id=user.user_id,
            jwt_token=jwt_token,
            filters=body.filters,
            limit=body.limit,
            score_threshold=body.scoreThreshold,
            evidence_mode=body.evidenceMode,
            document_id=body.documentId,
            cursor=body.cursor,
            batch_size=body.batchSize,
            group_by_document=body.groupByDocument,
            page=body.page,
            page_size=body.pageSize,
            multi_query_discovery=body.multiQueryDiscovery,
            multi_query_max_queries=body.multiQueryMaxQueries,
            multi_query_concurrency=body.multiQueryConcurrency,
        )
        if body.evidenceMode == "scope_exhaustive" and body.responseProfile == "langflow":
            result = project_scope_exhaustive_for_langflow(result)
        return JSONResponse(result, status_code=200)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OpenSearchDiskSpaceError:
        return JSONResponse({"error": DISK_SPACE_ERROR_MESSAGE}, status_code=507)
    except Exception as e:
        error_msg = str(e)
        if "AuthenticationException" in error_msg or "access denied" in error_msg.lower():
            return JSONResponse({"error": error_msg}, status_code=403)
        else:
            return JSONResponse({"error": error_msg}, status_code=500)
