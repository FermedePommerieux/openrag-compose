import os
from typing import Annotated, Any
from urllib.parse import urlparse

import boto3
from fastapi import Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from dependencies import (
    get_chat_service,
    get_current_user,
    get_docling_service,
    get_document_service,
    get_models_service,
    get_session_manager,
    get_task_service,
    require_all_permissions,
    require_permission,
)
from models.source_provenance import SourceProvenance
from session_manager import User
from utils.logging_config import get_logger

logger = get_logger(__name__)


class UploadPathBody(BaseModel):
    path: str | None = None
    replace_duplicates: bool = False
    archive_sources: bool | None = None
    source_provenance: SourceProvenance | None = None


class UploadBucketBody(BaseModel):
    s3_url: str
    replace_duplicates: bool = False


async def upload(
    file: Annotated[UploadFile, File(...)],
    document_service: Annotated[Any, Depends(get_document_service)],
    session_manager: Annotated[Any, Depends(get_session_manager)],
    user: Annotated[User, Depends(require_permission("knowledge:upload"))],
):
    """Upload a single file"""
    try:
        from config.settings import is_no_auth_mode

        is_no_auth = is_no_auth_mode()
        owner_user_id = user.user_id if (user and not is_no_auth) else None
        owner_name = user.name if user else None
        owner_email = user.email if user else None

        result = await document_service.process_upload_file(
            file,
            owner_user_id=owner_user_id,
            jwt_token=user.jwt_token,
            owner_name=owner_name,
            owner_email=owner_email,
        )
        return JSONResponse(result, status_code=201)
    except Exception as e:
        error_msg = str(e)
        if "AuthenticationException" in error_msg or "access denied" in error_msg.lower():
            logger.warning("[INGEST] Upload rejected — access denied", error=error_msg)
            return JSONResponse({"error": error_msg}, status_code=403)
        else:
            logger.exception("[INGEST] Upload failed")
            return JSONResponse({"error": error_msg}, status_code=500)


async def upload_path(
    body: UploadPathBody,
    task_service: Annotated[Any, Depends(get_task_service)],
    session_manager: Annotated[Any, Depends(get_session_manager)],
    user: Annotated[User, Depends(require_permission("knowledge:upload"))],
):
    """Ingest local paths and consume each source after successful indexing."""
    from config.settings import is_no_auth_mode
    from services.local_source_service import is_local_storage_available

    if not is_local_storage_available():
        return JSONResponse(
            {
                "error": (
                    "Local path ingestion is unavailable; use the multipart document ingestion API"
                )
            },
            status_code=403,
        )

    from services.local_source_service import (
        build_local_file_provenance,
        collect_ingest_files,
        resolve_ingestion_path,
        with_local_relative_path,
    )

    storage = None
    if not is_no_auth_mode():
        from services.user_storage_service import get_user_storage

        storage = await get_user_storage(user.db_user_id or user.user_id)
    resolved_path = resolve_ingestion_path(
        body.path, ingestion_root=storage.ingestion if storage else None
    )
    if resolved_path is None or not resolved_path.exists():
        return JSONResponse(
            {"error": "path must be inside your ingestion directory"},
            status_code=400,
        )

    file_paths = collect_ingest_files(resolved_path)

    if not file_paths:
        return JSONResponse({"error": "No files found in directory"}, status_code=400)

    jwt_token = user.jwt_token

    is_no_auth = is_no_auth_mode()
    owner_user_id = user.user_id if (user and not is_no_auth) else None
    owner_name = user.name if user else None
    owner_email = user.email if user else None

    from api.documents import _ensure_index_exists

    await _ensure_index_exists(jwt_token)

    from services.local_source_service import is_source_archiving_enabled

    archive_sources = (
        body.archive_sources if body.archive_sources is not None else is_source_archiving_enabled()
    )

    source_provenances: dict[str, SourceProvenance] = {}
    if body.source_provenance is not None:
        if len(file_paths) != 1:
            return JSONResponse(
                {
                    "error": (
                        "source_provenance can only be supplied when path resolves "
                        "to exactly one file"
                    )
                },
                status_code=400,
            )
        from models.source_provenance import parse_source_provenance

        try:
            provenance = parse_source_provenance(body.source_provenance)
        except ValueError as error:
            return JSONResponse({"error": f"invalid source_provenance: {error}"}, status_code=400)
        if provenance is not None:
            generated = build_local_file_provenance(file_paths[0], resolved_path)
            source_provenances[file_paths[0]] = with_local_relative_path(
                provenance,
                generated.relative_path or os.path.basename(file_paths[0]),
            )

    # Folder ingestion owns the relative filesystem context. When a caller did
    # not provide a richer identity, synthesize a portable file entity and a
    # shared directory-collection relation for every discovered source.
    for file_path in file_paths:
        source_provenances.setdefault(
            file_path,
            build_local_file_provenance(file_path, resolved_path),
        )

    task_id = await task_service.create_upload_task(
        owner_user_id,
        file_paths,
        jwt_token=jwt_token,
        owner_name=owner_name,
        owner_email=owner_email,
        replace_duplicates=body.replace_duplicates,
        archive_sources=archive_sources,
        source_provenances=source_provenances,
        cleanup_files=False,
        delete_source_after_success=True,
    )

    return JSONResponse(
        {
            "task_id": task_id,
            "total_files": len(file_paths),
            "status": "accepted",
            "archive_sources": archive_sources,
        },
        status_code=201,
    )


async def upload_context(
    file: Annotated[UploadFile, File(...)],
    document_service: Annotated[Any, Depends(get_document_service)],
    chat_service: Annotated[Any, Depends(get_chat_service)],
    session_manager: Annotated[Any, Depends(get_session_manager)],
    user: Annotated[User, Depends(require_all_permissions(("knowledge:upload", "chat:use")))],
    previous_response_id: Annotated[str | None, Form()] = None,
    endpoint: Annotated[str, Form()] = "langflow",
):
    """Upload a file and add its content as context to the current conversation"""
    filename = file.filename or "uploaded_document"
    user_id = user.user_id if user else None
    storage_user_id = (getattr(user, "db_user_id", None) or user.user_id) if user else None

    if previous_response_id and storage_user_id:
        from api.chat import _assert_owns

        await _assert_owns(previous_response_id, storage_user_id)

    jwt_token = user.jwt_token

    doc_result = await document_service.process_upload_context(
        file, filename, user_id=user_id, jwt_token=jwt_token
    )

    from config.settings import is_no_auth_mode

    is_no_auth = is_no_auth_mode()
    owner_user_id = user.user_id if (user and not is_no_auth) else None
    owner_name = user.name if user else None
    owner_email = user.email if user else None

    response_text, response_id = await chat_service.upload_context_chat(
        doc_result["content"],
        filename,
        user_id=user_id,
        jwt_token=jwt_token,
        previous_response_id=previous_response_id,
        endpoint=endpoint,
        owner=owner_user_id,
        owner_name=owner_name,
        owner_email=owner_email,
        storage_user_id=storage_user_id,
    )

    response_data = {
        "status": "context_added",
        "filename": doc_result["filename"],
        "pages": doc_result["pages"],
        "content_length": doc_result["content_length"],
        "response_id": response_id,
        "confirmation": response_text,
    }

    return JSONResponse(response_data)


async def upload_options(
    user: Annotated[User, Depends(get_current_user)],
):
    """Return availability of upload features"""
    aws_enabled = bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))
    from config.paths import get_documents_path
    from config.settings import UPLOAD_BATCH_SIZE, is_no_auth_mode
    from services.local_source_service import is_local_storage_available

    local_path_ingestion_enabled = is_local_storage_available()
    response: dict[str, object] = {
        "aws": aws_enabled,
        "upload_batch_size": UPLOAD_BATCH_SIZE,
        "local_path_ingestion_enabled": local_path_ingestion_enabled,
    }
    if local_path_ingestion_enabled:
        if is_no_auth_mode():
            response["documents_path"] = str(os.path.abspath(get_documents_path()))
        else:
            from services.user_storage_service import get_user_storage

            storage = await get_user_storage(user.db_user_id or user.user_id)
            response["documents_path"] = str(storage.ingestion)
    return JSONResponse(response)


async def upload_bucket(
    body: UploadBucketBody,
    task_service: Annotated[Any, Depends(get_task_service)],
    models_service: Annotated[Any, Depends(get_models_service)],
    docling_service: Annotated[Any, Depends(get_docling_service)],
    session_manager: Annotated[Any, Depends(get_session_manager)],
    user: Annotated[User, Depends(require_permission("knowledge:upload"))],
):
    """Process all files from an S3 bucket URL"""
    if not os.getenv("AWS_ACCESS_KEY_ID") or not os.getenv("AWS_SECRET_ACCESS_KEY"):
        return JSONResponse({"error": "AWS credentials not configured"}, status_code=400)

    if not body.s3_url or not body.s3_url.startswith("s3://"):
        return JSONResponse({"error": "Invalid S3 URL"}, status_code=400)

    parsed = urlparse(body.s3_url)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")

    s3_client = boto3.client("s3")
    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/"):
                keys.append(key)

    if not keys:
        return JSONResponse({"error": "No files found in bucket"}, status_code=400)

    jwt_token = user.jwt_token

    from config.settings import is_no_auth_mode
    from models.processors import S3FileProcessor

    is_no_auth = is_no_auth_mode()
    owner_user_id = user.user_id if (user and not is_no_auth) else None
    owner_name = user.name if user else None
    owner_email = user.email if user else None
    task_user_id = user.user_id if (user and not is_no_auth) else None

    from api.documents import _ensure_index_exists

    await _ensure_index_exists(jwt_token)

    processor = S3FileProcessor(
        task_service.document_service,
        bucket,
        models_service=models_service,
        docling_service=docling_service,
        s3_client=s3_client,
        owner_user_id=owner_user_id,
        jwt_token=jwt_token,
        owner_name=owner_name,
        owner_email=owner_email,
        replace_duplicates=body.replace_duplicates,
    )

    task_id = await task_service.create_custom_task(task_user_id, keys, processor)

    return JSONResponse(
        {"task_id": task_id, "total_files": len(keys), "status": "accepted"},
        status_code=201,
    )
