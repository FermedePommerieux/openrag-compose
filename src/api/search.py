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

logger = get_logger(__name__)


class SearchBody(BaseModel):
    query: str
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = 10
    scoreThreshold: float = Field(default=0, alias="scoreThreshold")
    evidenceMode: Literal["focused", "audit", "exhaustive"] = Field(
        default="focused", alias="evidenceMode"
    )
    documentId: str | None = Field(default=None, alias="documentId")
    cursor: str = ""
    batchSize: int = Field(default=20, ge=1, le=50, alias="batchSize")
    progressId: str | None = Field(default=None, max_length=64, alias="progressId")

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
        )

        # Langflow invokes /search in a separate HTTP request. Re-establish the
        # audit scope here so every nested reasoning and embedding response is
        # charged to the durable chat job that caused it.
        from services.token_usage_service import token_usage_service

        with token_usage_service.scope(body.progressId):
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
                audit_progress_id=body.progressId,
            )
        return JSONResponse(result, status_code=200)
    except ValueError as e:
        from services.audit_progress_service import audit_progress_service

        audit_progress_service.fail(body.progressId)
        return JSONResponse({"error": str(e)}, status_code=400)
    except OpenSearchDiskSpaceError:
        from services.audit_progress_service import audit_progress_service

        audit_progress_service.fail(body.progressId)
        return JSONResponse({"error": DISK_SPACE_ERROR_MESSAGE}, status_code=507)
    except Exception as e:
        from services.audit_progress_service import audit_progress_service

        audit_progress_service.fail(body.progressId)
        error_msg = str(e)
        if "AuthenticationException" in error_msg or "access denied" in error_msg.lower():
            return JSONResponse({"error": error_msg}, status_code=403)
        else:
            return JSONResponse({"error": error_msg}, status_code=500)
