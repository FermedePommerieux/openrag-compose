"""Shared backend-owned OpenSearch document indexing.

Langflow can generate chunks and embeddings, but it must not hold credentials
that can write arbitrary documents. This writer is the single backend path for
indexing chunks into the documents index.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from utils.embedding_fields import ensure_embedding_field_exists
from utils.embeddings import create_index_body
from utils.group_acl import unique_acl_principal_labels, unique_acl_principals
from utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class DocumentIndexContext:
    document_id: str
    mimetype: str
    embedding_model: str
    filename: str | None = None
    owner: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    file_size: int | None = None
    connector_type: str | None = None
    source_url: str | None = None
    connector_file_id: str | None = None
    allowed_users: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)
    allowed_principals: list[str] = field(default_factory=list)
    allowed_principal_labels: list[dict[str, Any]] = field(default_factory=list)
    ingest_run_id: str | None = None
    is_sample_data: bool = False
    index_name: str | None = None
    parser: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    chunking_strategy: str | None = None
    chunking_config_fingerprint: str | None = None


@dataclass
class DocumentIndexChunk:
    chunk_id: str
    text: str
    vector: list[float]
    page: int | None = None
    chunk_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentIndexWriter:
    """Write document chunks with a trusted backend OpenSearch client."""

    def __init__(self, opensearch_client: Any | None = None):
        self.opensearch_client = opensearch_client

    def _get_write_client(self) -> Any:
        from config.settings import clients

        client = self.opensearch_client or clients.opensearch
        if client is None:
            raise RuntimeError(
                "Backend OpenSearch write client is unavailable; cannot index document chunks"
            )
        return client

    async def index_chunks(
        self,
        context: DocumentIndexContext,
        chunks: list[DocumentIndexChunk],
        *,
        final: bool = False,
        refresh: bool | str = False,
    ) -> dict[str, Any]:
        """Index one batch of chunks.

        Repeated calls with the same chunk ids in the same ownership scope are
        idempotent because the write operation is an index/upsert.
        """
        from config.settings import get_index_name

        if not chunks:
            if final:
                await self._refresh(context.index_name or get_index_name())
            return {"indexed_chunks": 0, "ingest_run_id": context.ingest_run_id}

        first_vector = chunks[0].vector
        if not first_vector:
            raise ValueError("Cannot index chunks with empty embeddings")

        dimensions = len(first_vector)
        client = self._get_write_client()
        index_name = context.index_name or get_index_name()
        embedding_field = await self._ensure_index_and_embedding_field(
            client,
            index_name=index_name,
            embedding_model=context.embedding_model,
            dimensions=dimensions,
        )

        now = datetime.datetime.now(datetime.UTC).isoformat()
        bulk_body: list[dict[str, Any]] = []
        for chunk in chunks:
            if len(chunk.vector) != dimensions:
                raise ValueError(
                    "Embedding dimension mismatch in batch: "
                    f"expected {dimensions}, got {len(chunk.vector)} for {chunk.chunk_id}"
                )
            bulk_body.append(
                {
                    "index": {
                        "_index": index_name,
                        "_id": self.storage_chunk_id(context, chunk.chunk_id),
                    }
                }
            )
            bulk_body.append(
                self._build_chunk_document(
                    context=context,
                    chunk=chunk,
                    embedding_field=embedding_field,
                    indexed_time=now,
                )
            )

        result = await client.bulk(body=bulk_body, refresh=refresh)
        self._raise_for_bulk_errors(result)
        if final:
            await self._refresh(index_name)

        logger.info(
            "Indexed document chunks",
            index_name=index_name,
            document_id=context.document_id,
            ingest_run_id=context.ingest_run_id,
            chunk_count=len(chunks),
            final=final,
        )
        return {
            "indexed_chunks": len(chunks),
            "ingest_run_id": context.ingest_run_id,
            "document_id": context.document_id,
        }

    @staticmethod
    def _logical_chunk_id(context: DocumentIndexContext, chunk_id: str) -> str:
        """Return the persistent logical identity for one scoped chunk."""
        scope = "shared" if context.owner is None else f"owner:{context.owner}"
        scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
        return f"{scope_digest}_{chunk_id}"

    @classmethod
    def storage_chunk_id(cls, context: DocumentIndexContext, chunk_id: str) -> str:
        """Return the physical id, isolating an in-progress generation.

        The source ``chunk_id`` remains logical and stable for RRF.  A
        temporary ingest run changes only the storage id, so a failed replace
        cannot overwrite the currently promoted generation.
        """
        logical_id = cls._logical_chunk_id(context, chunk_id)
        if context.ingest_run_id:
            return f"{logical_id}__run_{context.ingest_run_id}"
        return logical_id

    async def delete_ingest_run(
        self,
        ingest_run_id: str,
        *,
        index_name: str | None = None,
        document_id: str | None = None,
        owner: str | None = None,
        shared: bool = False,
    ) -> int:
        """Delete only this failed generation in its ownership scope.

        An ingest run id is random, but cleanup uses the administrative writer
        and must still carry the document/owner boundary.  Content hashes and
        filenames are not globally unique across workspaces or users.
        """
        if not ingest_run_id:
            return 0
        if not document_id:
            raise ValueError("Refusing unscoped failed-ingest cleanup without document_id")
        if shared and owner is not None:
            raise ValueError("A shared failed-ingest cleanup cannot specify an owner")
        if not shared and owner is None:
            raise ValueError("Refusing unscoped failed-ingest cleanup without an owner")
        from config.settings import get_index_name

        client = self._get_write_client()
        resolved_index = index_name or get_index_name()
        filters: list[dict[str, Any]] = [
            {"term": {"ingest_run_id": ingest_run_id}},
            {"term": {"document_id": document_id}},
        ]
        if shared:
            filters.append({"bool": {"must_not": {"exists": {"field": "owner"}}}})
        else:
            filters.append({"term": {"owner": owner}})
        body = {"query": {"bool": {"filter": filters}}}
        response = await client.delete_by_query(
            index=resolved_index,
            body=body,
            refresh=True,
            conflicts="proceed",
        )
        deleted = int(response.get("deleted", 0)) if isinstance(response, dict) else 0
        logger.info(
            "Deleted failed ingest run chunks",
            index_name=resolved_index,
            ingest_run_id=ingest_run_id,
            deleted=deleted,
        )
        return deleted

    async def _ensure_index_and_embedding_field(
        self,
        client: Any,
        *,
        index_name: str,
        embedding_model: str,
        dimensions: int,
    ) -> str:
        if not await client.indices.exists(index=index_name):
            await client.indices.create(
                index=index_name,
                body=await create_index_body(embedding_model, dimensions),
            )
        await self._ensure_retrieval_metadata_fields(client, index_name)
        return await ensure_embedding_field_exists(
            client,
            embedding_model,
            index_name,
            dimensions,
        )

    @staticmethod
    async def _ensure_retrieval_metadata_fields(client: Any, index_name: str) -> None:
        """Add v2 provenance fields to an index created before this release.

        Mapping additions are additive and safe for existing chunks.  A field
        dynamically created with an incompatible type cannot be changed in
        place by OpenSearch, so we log an actionable warning instead of hiding
        the mismatch or silently reindexing user data.
        """
        required = {
            # OpenSearch does not support sorting on its metadata ``_id``.
            # Persist the scoped logical chunk id instead: it is deterministic
            # across equivalent re-indexes and has keyword doc_values for RRF's
            # secondary sort.  Legacy chunks may not have it; query code makes
            # that degraded ordering explicit rather than rewriting user data.
            "chunk_id": {"type": "keyword"},
            "chunking_config_fingerprint": {"type": "keyword"},
            "connector_file_id": {"type": "keyword"},
            "chunk_index": {"type": "integer"},
            "chunking_strategy": {"type": "keyword"},
            "parser": {"type": "keyword"},
            "chunk_size": {"type": "integer"},
            "chunk_overlap": {"type": "integer"},
        }
        try:
            mappings = await client.indices.get_mapping(index=index_name)
            properties: dict[str, Any] = {}
            for mapping in mappings.values():
                candidate = mapping.get("mappings", {}).get("properties", {})
                if isinstance(candidate, dict):
                    properties.update(candidate)
            missing = {
                name: definition
                for name, definition in required.items()
                if name not in properties
            }
            incompatible = {
                name: properties[name].get("type")
                for name, definition in required.items()
                if name in properties and properties[name].get("type") != definition["type"]
            }
            if incompatible:
                logger.warning(
                    "Existing retrieval provenance mapping is incompatible; reindex required",
                    index_name=index_name,
                    fields=incompatible,
                )
            if missing:
                await client.indices.put_mapping(index=index_name, body={"properties": missing})
                logger.info(
                    "Added retrieval provenance mapping fields",
                    index_name=index_name,
                    fields=list(missing),
                )
        except Exception as exc:
            # The following embedding-field check is authoritative for write
            # safety; do not make an additive metadata enhancement block an
            # otherwise valid existing index.
            logger.warning(
                "Unable to ensure retrieval provenance mapping fields",
                index_name=index_name,
                error=str(exc),
            )

    def _build_chunk_document(
        self,
        *,
        context: DocumentIndexContext,
        chunk: DocumentIndexChunk,
        embedding_field: str,
        indexed_time: str,
    ) -> dict[str, Any]:
        metadata = self._normalized_metadata(chunk.metadata)
        document_id = context.document_id or str(metadata.get("document_id") or chunk.chunk_id)
        filename = context.filename or str(metadata.get("filename") or "")
        mimetype = context.mimetype or str(metadata.get("mimetype") or "")

        doc: dict[str, Any] = {
            "chunk_id": self._logical_chunk_id(context, chunk.chunk_id),
            "document_id": document_id,
            "filename": filename,
            "mimetype": mimetype,
            "page": chunk.page if chunk.page is not None else metadata.get("page", 0),
            "chunk_index": (
                chunk.chunk_index
                if chunk.chunk_index is not None
                else metadata.get("chunk_index")
            ),
            "text": chunk.text,
            embedding_field: chunk.vector,
            "embedding_model": context.embedding_model,
            "embedding_dimensions": len(chunk.vector),
            "file_size": context.file_size
            if context.file_size is not None
            else metadata.get("file_size"),
            "connector_type": context.connector_type or metadata.get("connector_type") or "local",
            "source_url": context.source_url or metadata.get("source_url") or "",
            "allowed_users": list(context.allowed_users),
            "allowed_groups": list(context.allowed_groups),
            "allowed_principals": unique_acl_principals(context.allowed_principals),
            "allowed_principal_labels": unique_acl_principal_labels(
                context.allowed_principal_labels
            ),
            "indexed_time": indexed_time,
            "metadata": metadata.get("metadata", {}),
        }

        parser = context.parser or metadata.get("parser")
        if parser:
            doc["parser"] = parser

        chunking_strategy = context.chunking_strategy or metadata.get("chunking_strategy")
        if chunking_strategy:
            doc["chunking_strategy"] = str(chunking_strategy)
        if context.chunking_config_fingerprint:
            doc["chunking_config_fingerprint"] = context.chunking_config_fingerprint

        for field_name in ("chunk_size", "chunk_overlap"):
            context_value = getattr(context, field_name)
            value = context_value if context_value is not None else metadata.get(field_name)
            if value is None:
                continue
            try:
                doc[field_name] = int(value)
            except (TypeError, ValueError):
                # Skip assignment if coercion fails to avoid type conflicts
                pass

        if context.owner is not None:
            doc["owner"] = context.owner
        if context.owner_name is not None:
            doc["owner_name"] = context.owner_name
        if context.owner_email is not None:
            doc["owner_email"] = context.owner_email
        if context.ingest_run_id:
            doc["ingest_run_id"] = context.ingest_run_id
        if context.connector_file_id:
            doc["connector_file_id"] = context.connector_file_id
        elif metadata.get("connector_file_id"):
            doc["connector_file_id"] = metadata["connector_file_id"]
        if context.is_sample_data:
            doc["is_sample_data"] = "true"
        for time_field in ("created_time", "modified_time"):
            if metadata.get(time_field):
                doc[time_field] = metadata[time_field]

        return doc

    @staticmethod
    def _normalized_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(metadata or {})
        for key in (
            "allowed_users",
            "allowed_groups",
            "allowed_principals",
            "allowed_principal_labels",
        ):
            value = normalized.get(key)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, list):
                    normalized[key] = parsed
        if "filesize" in normalized and "file_size" not in normalized:
            normalized["file_size"] = normalized["filesize"]
        return normalized

    @staticmethod
    def _raise_for_bulk_errors(result: Any) -> None:
        if not isinstance(result, dict) or not result.get("errors"):
            return
        # Keep only the items that actually failed (a bulk response interleaves
        # successes and failures) and carry their full OpenSearch error body so
        # the cause — e.g. a mapper_parsing_exception naming the offending field
        # — survives in the raised error. The caller logs this once with request
        # context; this helper only raises so the failure isn't logged twice.
        failures = []
        for item in result.get("items", []):
            action = item.get("index") or item.get("create") or item.get("update") or item
            if not action.get("error"):
                continue
            failures.append(
                {
                    "id": action.get("_id"),
                    "status": action.get("status"),
                    "error": action.get("error"),
                }
            )
            if len(failures) >= 5:
                break
        if not failures:
            # `errors` was set but no item carried an error body (rare/contradictory);
            # fall back to the first few raw items so the message keeps some detail.
            failures = result.get("items", [])[:3]
        raise RuntimeError(f"OpenSearch bulk indexing failed: {failures}")

    async def _refresh(self, index_name: str) -> None:
        client = self._get_write_client()
        await client.indices.refresh(index=index_name)
