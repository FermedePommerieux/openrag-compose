import asyncio
import hashlib
import json
import mimetypes
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from config.settings import clients, get_embedding_model, get_index_name, get_openrag_config
from session_manager import AnonymousUser
from utils.document_processing import (
    HYBRID_CHUNKING_SCHEMA_VERSION,
    HybridChunkingError,
    chunk_docling_hybrid,
    extract_relevant,
    process_text_file,
    resplit_chunks_character_windows,
    split_chunks_by_max_tokens,
)
from utils.file_utils import (
    auto_cleanup_tempfile,
    clean_connector_filename,
    get_file_extension,
    get_filename_aliases,
    langflow_safe_filename_and_mimetype,
)
from utils.hash_utils import hash_id
from utils.logging_config import get_logger
from utils.opensearch_queries import build_filename_search_body, build_replace_filename_query

from .tasks import FileTask, TaskStatus, UploadTask

logger = get_logger(__name__)

DOCLING_PARSER_LABEL = "Docling Serve 1.20.0"
TEXT_PARSER_LABEL = "Text Parser"
DUPLICATE_FILENAME_WARNING = "A file with this name already exists."

if TYPE_CHECKING:
    from connectors.base import DocumentACL
    from models.source_provenance import SourceProvenance


def _require_chunking_strategy(config: Any) -> Literal["character", "hybrid"]:
    """Return an explicitly configured, supported internal chunking strategy."""
    knowledge_config = getattr(config, "knowledge", None)
    configured_strategy = getattr(knowledge_config, "chunking_strategy", None)
    if isinstance(configured_strategy, str):
        if configured_strategy == "character":
            return "character"
        if configured_strategy == "hybrid":
            return "hybrid"
    raise ValueError(
        "Invalid internal knowledge configuration: knowledge.chunking_strategy "
        "must be explicitly set to 'character' or 'hybrid'"
    )


def _verification_client(fallback_client):
    """Client for post-ingestion verification ("did the chunks land in the
    index?"). That is a system integrity check, not a user-visibility check,
    so prefer the platform writer client: it does not depend on the JWT/JWKS
    trust chain that user-scoped clients need (OpenSearch loads the backend's
    JWKS lazily, so the first user-JWT queries after a cold start can 401).
    Falls back to the caller's client when the writer is unavailable."""
    return clients.opensearch if clients.opensearch is not None else fallback_client


def resolve_shared_owner_fields(
    user_id: str | None,
    owner_name: str | None,
    owner_email: str | None,
    shared: bool,
) -> tuple[str | None, str | None, str | None]:
    """Return (owner, owner_name, owner_email) for indexing.

    When shared=True, owner is None so the indexed chunk omits the owner field
    entirely, triggering the OpenSearch DLS must_not-exists-owner clause that
    makes the document visible to all users in the instance. owner_name and
    owner_email are set to AnonymousUser values, matching how default/sample
    documents are loaded.
    """
    if shared:
        _anon = AnonymousUser()
        return None, _anon.name, _anon.email
    return user_id, owner_name, owner_email


class TaskProcessor:
    """Base class for task processors with shared processing logic"""

    def __init__(self, document_service=None, models_service=None, docling_service=None):
        self.document_service = document_service
        self.models_service = models_service
        self.docling_service = docling_service

    async def check_document_exists(
        self,
        file_hash: str,
        opensearch_client,
        on_error: Literal["assume_missing", "assume_exists"] = "assume_missing",
        *,
        wait_for_visibility: bool = False,
        field: str = "document_id",
        owner_user_id: str | None = None,
        shared: bool | None = None,
    ) -> bool:
        """
        Check if a document with the given hash already exists in OpenSearch.
        Consolidated hash checking for all processors.

        ``on_error`` picks the answer when OpenSearch stays unreachable after
        retries — the check is ambiguous then, and the safe default differs by
        caller:
          * ``"assume_missing"`` (dedupe callers): safer to reprocess than skip.
          * ``"assume_exists"`` (post-ingestion verification callers): an
            infrastructure error must not fail a file that Langflow already
            reported as ingested.

        When ``wait_for_visibility`` is True, an empty result is retried a few
        times with backoff before concluding the document is absent. This is for
        post-ingest verification: chunks that were just written may not be
        searchable yet within OpenSearch's near-real-time refresh window
        (default ~1s), and the user-scoped client cannot force an
        ``indices:admin/refresh`` (it lacks the privilege).
        """
        max_retries = 3
        retry_delay = 1.0

        # Some deployments' indices predate connector_file_id's addition to the
        # explicit mapping (config/settings.py), so OpenSearch dynamically
        # mapped it as analyzed `text` (with a `.keyword` multi-field) instead
        # of the intended `keyword` type. A plain term query against such a
        # field tokenizes the query value and rarely matches the raw id, so
        # also match its `.keyword` multi-field. document_id has always been
        # explicitly `keyword` since index creation and never has this issue.
        query: dict[str, Any]
        if field == "connector_file_id":
            field_query = {
                "bool": {
                    "should": [
                        {"term": {field: file_hash}},
                        {"term": {f"{field}.keyword": file_hash}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        else:
            field_query = {"term": {field: file_hash}}

        # Calls that provide ``shared`` deliberately opt into an ownership
        # boundary.  Preserve the historic unscoped behavior for legacy
        # read-only callers that do not provide a scope at all.
        if shared is None:
            query = field_query
        else:
            from utils.opensearch_queries import (
                build_anonymous_document_query,
                build_owned_document_query,
            )

            if field == "document_id":
                query = (
                    build_anonymous_document_query(file_hash)
                    if shared or owner_user_id is None
                    else build_owned_document_query(file_hash, owner_user_id)
                )
            else:
                owner_filter = (
                    {"bool": {"must_not": {"exists": {"field": "owner"}}}}
                    if shared or owner_user_id is None
                    else {"term": {"owner": owner_user_id}}
                )
                query = {"bool": {"filter": [field_query, owner_filter]}}

        for attempt in range(max_retries):
            try:
                response = await opensearch_client.search(
                    index=get_index_name(),
                    body={
                        "size": 1,
                        "_source": False,
                        "query": query,
                    },
                )
                hits = response.get("hits", {}).get("hits", [])
                if hits:
                    return True
                # No hits. For post-ingest verification, the document may not be
                # visible yet within the near-real-time refresh window — retry.
                if wait_for_visibility and attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                return False
            except (TimeoutError, Exception) as e:
                if attempt == max_retries - 1:
                    logger.error(
                        "OpenSearch exists check failed after retries",
                        file_hash=file_hash,
                        error=str(e),
                        attempt=attempt + 1,
                    )
                    if on_error == "assume_exists":
                        logger.warning(
                            "Exists check inconclusive due to connection issues; "
                            "assuming document exists",
                            file_hash=file_hash,
                        )
                        return True
                    # Safer to reprocess than skip for dedupe callers.
                    logger.warning(
                        "Assuming document doesn't exist due to connection issues",
                        file_hash=file_hash,
                    )
                    return False
                else:
                    logger.warning(
                        "OpenSearch exists check failed, retrying",
                        file_hash=file_hash,
                        error=str(e),
                        attempt=attempt + 1,
                        retry_in=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
        return on_error == "assume_exists"

    async def check_filename_exists(
        self,
        filename: str,
        opensearch_client,
        *,
        wait_for_visibility: bool = False,
    ) -> bool:
        """
        Check if a document with the given filename already exists in OpenSearch.
        Returns True if any chunks with this filename exist.

        When ``wait_for_visibility`` is True, an empty result is retried a few
        times with backoff before concluding the document is absent. This is for
        post-ingest verification: chunks that were just written may not be
        searchable yet within OpenSearch's near-real-time refresh window
        (default ~1s), and the user-scoped client cannot force an
        ``indices:admin/refresh`` (it lacks the privilege).
        """
        max_retries = 3
        retry_delay = 1.0

        candidate_filenames = get_filename_aliases(filename)
        if not candidate_filenames:
            return False
        # Keep track of aliases that still need checking across retries.
        # If one alias was already checked successfully with no hits, we avoid
        # re-querying it when another alias fails transiently.
        pending_candidates = list(candidate_filenames)
        # Retry strategy: only retry aliases that have not completed successfully.
        # This avoids re-querying aliases already checked with no hits when a later
        # alias fails transiently (e.g., timeout).

        for attempt in range(max_retries):
            try:
                i = 0
                while i < len(pending_candidates):
                    candidate = pending_candidates[i]
                    search_body = build_filename_search_body(candidate, size=1, source=False)
                    response = await opensearch_client.search(
                        index=get_index_name(), body=search_body
                    )
                    hits = response.get("hits", {}).get("hits", [])
                    if hits:
                        return True
                    # Successfully checked this alias with no hits; don't
                    # re-query it on future retries.
                    pending_candidates.pop(i)
                    continue
                # All aliases checked with no hits. For post-ingest verification,
                # the document may not be visible yet within the near-real-time
                # refresh window — re-check every alias after a short delay.
                if wait_for_visibility and attempt < max_retries - 1:
                    pending_candidates = list(candidate_filenames)
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                return False

            except (TimeoutError, Exception) as e:
                if attempt == max_retries - 1:
                    logger.error(
                        "OpenSearch filename check failed after retries",
                        filename=filename,
                        error=str(e),
                        attempt=attempt + 1,
                    )
                    # On final failure, assume document doesn't exist (safer to reprocess than skip)
                    logger.warning(
                        "Assuming filename doesn't exist due to connection issues",
                        filename=filename,
                    )
                    return False
                else:
                    logger.warning(
                        "OpenSearch filename check failed, retrying",
                        filename=filename,
                        error=str(e),
                        attempt=attempt + 1,
                        retry_in=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
        return False

    @staticmethod
    def _chunking_config_fingerprint(
        *,
        strategy: str,
        chunk_size: int | None,
        chunk_overlap: int | None,
        hybrid_max_tokens: int,
        hybrid_merge_peers: bool,
    ) -> str:
        """Hash the chunking inputs that determine a document generation."""
        payload = {
            "strategy": strategy,
            "chunk_size": chunk_size if strategy == "character" else None,
            "chunk_overlap": chunk_overlap if strategy == "character" else None,
            "hybrid_max_tokens": hybrid_max_tokens if strategy == "hybrid" else None,
            "hybrid_merge_peers": hybrid_merge_peers if strategy == "hybrid" else None,
            "hybrid_schema_version": (
                HYBRID_CHUNKING_SCHEMA_VERSION if strategy == "hybrid" else None
            ),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def check_document_matches_chunking(
        self,
        file_hash: str,
        opensearch_client,
        *,
        fingerprint: str,
        owner_user_id: str | None = None,
        shared: bool = False,
    ) -> bool:
        """Return true only for a generation indexed with the same settings.

        Legacy chunks without a fingerprint deliberately re-index once.  This
        makes a request to switch character to hybrid observable instead of
        silently treating a same-content document as unchanged.
        """
        from utils.opensearch_queries import (
            build_anonymous_document_query,
            build_owned_document_query,
        )

        # Identical bytes produce identical hashes.  Never let a user A's
        # generation make user B's same-content upload appear already indexed.
        # DLS is not enough here because later promotion uses an admin client.
        scope_query = (
            build_anonymous_document_query(file_hash)
            if shared or owner_user_id is None
            else build_owned_document_query(file_hash, owner_user_id)
        )
        try:
            response = await opensearch_client.search(
                index=get_index_name(),
                body={
                    "size": 1,
                    "_source": ["chunking_config_fingerprint"],
                    "query": scope_query,
                },
            )
        except Exception as exc:
            logger.warning("Chunking generation check failed; re-indexing safely", error=str(exc))
            return False
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return False
        source = hits[0].get("_source", {})
        return source.get("chunking_config_fingerprint") == fingerprint

    async def _promote_document_generation(
        self,
        *,
        opensearch_client,
        new_storage_ids: set[str],
        file_hash: str,
        filename: str,
        owner_user_id: str | None,
        shared: bool,
        replace_existing_filename: bool,
        connector_file_id: str | None,
        connector_type: str,
    ) -> None:
        """Delete prior scoped chunks only after a complete new generation exists."""
        from connectors.chunk_cleanup import build_connector_file_chunks_query
        from utils.opensearch_delete import collect_visible_document_hits, delete_document_ids
        from utils.opensearch_queries import (
            build_anonymous_document_query,
            build_anonymous_filename_query,
            build_owned_document_query,
            build_owned_filename_query,
            build_replace_filename_query,
        )

        write_client = clients.opensearch
        if write_client is None:
            raise RuntimeError("Backend OpenSearch write client is unavailable")

        # A content hash is a logical document identity, not an ownership
        # boundary.  The trusted writer can see every tenant's chunks, so every
        # destructive query begins with this owner/shared scope before optional
        # filename or connector identities are added.
        document_query = (
            build_anonymous_document_query(file_hash)
            if shared or owner_user_id is None
            else build_owned_document_query(file_hash, owner_user_id)
        )
        queries: list[dict[str, Any]] = [document_query]
        if replace_existing_filename:
            for candidate in get_filename_aliases(filename):
                if shared:
                    queries.append(build_replace_filename_query(candidate, owner_user_id))
                elif owner_user_id:
                    queries.append(build_owned_filename_query(candidate, owner_user_id))
                else:
                    queries.append(build_anonymous_filename_query(candidate))
        if connector_file_id:
            queries.append(
                build_connector_file_chunks_query(
                    [connector_file_id],
                    connector_type=connector_type,
                    owner_user_id=owner_user_id,
                    shared=shared,
                )
            )

        old_hits: dict[str, dict[str, Any]] = {}
        for query in queries:
            for hit in await collect_visible_document_hits(
                opensearch_client,
                index=get_index_name(),
                query=query,
                source=True,
            ):
                old_hits[hit["_id"]] = hit
        stale_ids = set(old_hits) - new_storage_ids
        if stale_ids:
            try:
                deleted_count = await delete_document_ids(
                    write_client,
                    index=get_index_name(),
                    document_ids=sorted(stale_ids),
                    # Refresh once after the bounded set of concrete deletes.
                    # Refresh-per-ID turns a 2,000-chunk replacement into 2,000
                    # index-wide refreshes and can stall compact clusters.
                    refresh=False,
                )
                if deleted_count != len(stale_ids):
                    raise RuntimeError(
                        "Generation promotion deleted an incomplete stale set: "
                        f"expected={len(stale_ids)}, deleted={deleted_count}"
                    )
                await write_client.indices.refresh(
                    index=get_index_name(),
                    request_timeout=300,
                )
            except Exception as delete_error:
                # Individual DLS-safe deletes can fail after a subset succeeds.
                # Restore the snapshot before surfacing the failure so a failed
                # promotion never leaves the previous generation partially lost.
                try:
                    await self._restore_document_snapshot(
                        write_client=write_client,
                        index_name=get_index_name(),
                        snapshot=old_hits,
                        document_ids=stale_ids,
                    )
                except Exception as restore_error:
                    # This is intentionally fatal.  A partial bulk success is
                    # not a rollback, and operators need the affected primary
                    # ids to recover without guessing which generation survived.
                    logger.error(
                        "Hybrid promotion rollback failed after partial delete",
                        error=str(restore_error),
                        delete_error=str(delete_error),
                        affected_chunk_ids=sorted(stale_ids),
                    )
                    raise RuntimeError(
                        "Hybrid promotion delete failed and rollback could not be verified; "
                        f"affected_chunk_ids={sorted(stale_ids)}"
                    ) from restore_error
                raise

    @staticmethod
    async def _restore_document_snapshot(
        *,
        write_client,
        index_name: str,
        snapshot: dict[str, dict[str, Any]],
        document_ids: set[str],
    ) -> None:
        """Restore and verify every old chunk after a failed promotion.

        OpenSearch bulk responses may be HTTP-successful while individual
        actions fail.  The previous generation remains the rollback contract,
        so treating ``errors=true`` or a missing item as success would hide a
        data-loss incident.
        """
        restore_body: list[dict[str, Any]] = []
        expected_ids: set[str] = set()
        for document_id in sorted(document_ids):
            source = snapshot.get(document_id, {}).get("_source")
            if not isinstance(source, dict):
                raise RuntimeError(f"Rollback snapshot is missing source for {document_id}")
            expected_ids.add(document_id)
            restore_body.extend([{"index": {"_index": index_name, "_id": document_id}}, source])
        if not restore_body:
            return

        response = await write_client.bulk(body=restore_body, refresh=True)
        if not isinstance(response, dict):
            raise RuntimeError("Rollback bulk response was not an object")
        failures: list[dict[str, Any]] = []
        restored_ids: set[str] = set()
        for item in response.get("items", []):
            action = item.get("index") if isinstance(item, dict) else None
            if not isinstance(action, dict):
                failures.append({"item": item})
                continue
            item_id = action.get("_id")
            if action.get("error") or int(action.get("status", 500)) not in {200, 201}:
                failures.append(
                    {"id": item_id, "status": action.get("status"), "error": action.get("error")}
                )
                continue
            if item_id:
                restored_ids.add(str(item_id))
        missing_ids = expected_ids - restored_ids
        if response.get("errors") or failures or missing_ids:
            raise RuntimeError(
                "Rollback bulk restore was incomplete: "
                f"failures={failures}, missing_chunk_ids={sorted(missing_ids)}"
            )

    async def resolve_duplicate_filename(
        self,
        filename: str,
        opensearch_client,
        *,
        replace: bool,
        owner_user_id: str | None,
        shared: bool = False,
    ) -> Literal["proceed", "skip", "replaced", "replace_pending"]:
        """Single duplicate-filename policy shared by every processor.

        Checks whether a document with this filename (or one of its aliases)
        is already indexed and applies the caller's replace decision:

          * ``"proceed"``  — no duplicate; continue ingestion.
          * ``"skip"``     — duplicate and ``replace`` is False; the caller
                             should finish via ``mark_duplicate_skipped``.
          * ``"replaced"`` — duplicate and ``replace`` is True; the existing
                             chunks were deleted and the index refreshed, so
                             ingestion can continue.
          * ``"replace_pending"`` — Hybrid ingestion retains the existing
                             generation until the backend promotes a validated
                             replacement.
        """
        if not await self.check_filename_exists(filename, opensearch_client):
            return "proceed"
        if not replace:
            return "skip"

        if _require_chunking_strategy(get_openrag_config()) == "hybrid":
            logger.info(
                "Deferring duplicate deletion until hybrid generation promotion", filename=filename
            )
            return "replace_pending"

        logger.info(f"Replacing existing document: {filename}")
        deleted = await self.delete_document_by_filename(
            filename,
            opensearch_client,
            owner_user_id=owner_user_id,
            shared=shared,
        )
        if deleted == 0:
            logger.warning(
                "Replacement requested but deletion removed no chunks",
                filename=filename,
            )
            return "skip"
        # Refresh so the delete is visible before re-ingest. refresh is
        # index-wide (indices:admin/refresh) and cannot be DLS-scoped, so it
        # must run under the admin/service client, not the user client.
        try:
            await clients.opensearch.indices.refresh(index=get_index_name())
        except Exception as refresh_error:
            logger.warning(
                "Failed to refresh index after delete",
                error=str(refresh_error),
            )
        return "replaced"

    def mark_duplicate_skipped(self, upload_task: UploadTask, file_task: FileTask) -> None:
        """Uniform terminal state for a duplicate that was not replaced:
        SKIPPED, counted toward successful files, with a warning the task view
        surfaces. A declined replacement is a chosen outcome, not an error."""
        file_task.status = TaskStatus.SKIPPED
        file_task.error = None
        file_task.result = {
            "status": "skipped",
            "reason": "duplicate_filename",
            "warning": DUPLICATE_FILENAME_WARNING,
        }
        file_task.updated_at = time.time()
        upload_task.successful_files += 1

    async def delete_document_by_filename(
        self,
        filename: str,
        opensearch_client,
        owner_user_id: str | None = None,
        shared: bool = False,
    ) -> int:
        """Delete all chunks for a filename and return the deleted count."""
        from config.settings import clients, get_index_name
        from utils.opensearch_delete import collect_visible_document_ids, delete_document_ids
        from utils.opensearch_queries import (
            build_anonymous_filename_query,
            build_owned_filename_query,
            build_replace_filename_query,
        )

        try:
            write_client = clients.opensearch
            if write_client is None:
                raise RuntimeError("Backend OpenSearch write client is unavailable")

            if not owner_user_id:
                if shared:

                    def build_query(fname, _owner):
                        return build_anonymous_filename_query(fname)

                else:
                    logger.warning(
                        "Skipped delete_by_filename because owner_user_id is missing",
                        filename=filename,
                    )
                    return 0
            else:
                build_query = build_replace_filename_query if shared else build_owned_filename_query

            candidate_filenames = get_filename_aliases(filename)
            if not candidate_filenames:
                logger.info(
                    "Skipped delete_by_filename because filename input is empty",
                    filename=filename,
                )
                return 0

            deleted_count = 0
            for candidate in candidate_filenames:
                document_ids = await collect_visible_document_ids(
                    opensearch_client,
                    index=get_index_name(),
                    query=build_query(candidate, owner_user_id),
                )
                deleted_count += await delete_document_ids(
                    write_client,
                    index=get_index_name(),
                    document_ids=document_ids,
                )
            logger.info(
                "Deleted existing document chunks", filename=filename, deleted_count=deleted_count
            )
            return deleted_count

        except Exception as e:
            logger.error("Failed to delete existing document", filename=filename, error=str(e))
            raise

    async def _delete_connector_chunks(
        self,
        file_id: str,
        opensearch_client,
        owner_user_id: str,
        keep_filenames: list[str] | None = None,
        shared: bool = False,
        connector_type: str | None = None,
    ) -> int:
        """Delete indexed chunks for a connector file by its STABLE id.

        Deletion semantics live in ``connectors.chunk_cleanup``. This wrapper
        stays best-effort so a cleanup miss never fails the surrounding task.
        """
        from connectors.chunk_cleanup import delete_connector_file_chunks

        if not file_id:
            return 0
        try:
            return await delete_connector_file_chunks(
                [file_id],
                opensearch_client,
                connector_type=connector_type,
                owner_user_id=owner_user_id,
                shared=shared,
                keep_filenames=keep_filenames,
            )
        except Exception as e:
            logger.error(
                "Failed to delete connector chunks",
                file_id=file_id,
                error=str(e),
            )
            return 0

    async def process_document_standard(
        self,
        file_path: str,
        file_hash: str,
        owner_user_id: str = None,
        original_filename: str = None,
        jwt_token: str = None,
        owner_name: str = None,
        owner_email: str = None,
        file_size: int = None,
        connector_type: str = "local",
        embedding_model: str = None,
        chunk_size: int = None,
        chunk_overlap: int = None,
        is_sample_data: bool = False,
        acl: "DocumentACL | None" = None,
        connector_file_id: str | None = None,
        ocr: bool | None = None,
        picture_descriptions: bool | None = None,
        shared: bool = False,
        source_url: str | None = None,
        source_provenance: "SourceProvenance | None" = None,
        replace_existing_filename: bool = False,
        replace_connector_file_id: bool = False,
        force_reprocess: bool = False,
    ):
        """
        Standard processing pipeline for non-Langflow processors:
        docling conversion + embeddings + OpenSearch indexing.

        Args:
            embedding_model: Embedding model to use (defaults to the current
                embedding model from settings)
            chunk_size: Optional character window size for re-splitting extracted
                chunks (non-Langflow path, e.g. connector UI ``chunkSize``).
            chunk_overlap: Overlap between windows; must be less than ``chunk_size``.
            acl: DocumentACL instance with access control information
            ocr: Per-request OCR override (None = use global config).
            picture_descriptions: Per-request picture descriptions override.
            source_provenance: Validated W3C PROV-O identity and relations.
                It is document context and is repeated on every indexed chunk.
        """
        from services.document_service import chunk_texts_for_embeddings

        # Use provided embedding model or configured model.
        # get_embedding_model() returns empty string when Langflow ingest is enabled,
        # but OpenRAG processors still need a concrete embedding model.
        config = get_openrag_config()
        configured_embedding_model = config.knowledge.embedding_model
        embedding_model = embedding_model or configured_embedding_model or get_embedding_model()

        if chunk_size is None:
            chunk_size = getattr(config.knowledge, "chunk_size", 1000)
        if chunk_overlap is None:
            chunk_overlap = getattr(config.knowledge, "chunk_overlap", 200)
        try:
            chunk_size = int(chunk_size)
        except (TypeError, ValueError):
            chunk_size = 1000
        try:
            chunk_overlap = int(chunk_overlap)
        except (TypeError, ValueError):
            chunk_overlap = 200
        requested_chunking_strategy = _require_chunking_strategy(config)
        configured_hybrid_max_tokens = getattr(config.knowledge, "hybrid_max_tokens", 512)
        try:
            hybrid_max_tokens = max(1, int(configured_hybrid_max_tokens))
        except (TypeError, ValueError):
            hybrid_max_tokens = 512
        configured_merge_peers = getattr(config.knowledge, "hybrid_merge_peers", True)
        hybrid_merge_peers = (
            configured_merge_peers if isinstance(configured_merge_peers, bool) else True
        )
        effective_chunking_strategy = "character"
        chunking_fingerprint = self._chunking_config_fingerprint(
            strategy=requested_chunking_strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            hybrid_max_tokens=hybrid_max_tokens,
            hybrid_merge_peers=hybrid_merge_peers,
        )

        # Get user's OpenSearch client with JWT for OIDC auth
        opensearch_client = self.document_service.session_manager.get_user_opensearch_client(
            owner_user_id, jwt_token
        )

        # Same bytes are unchanged only when the chunking generation also
        # matches. A character-indexed document must be eligible for an
        # explicit hybrid request, and legacy chunks without a fingerprint are
        # re-indexed once rather than silently preserved.
        if not force_reprocess and await self.check_document_matches_chunking(
            file_hash,
            opensearch_client,
            fingerprint=chunking_fingerprint,
            owner_user_id=owner_user_id,
            shared=shared,
        ):
            return {"status": "unchanged", "id": file_hash}

        logger.info(
            "Processing document with embedding model",
            embedding_model=embedding_model,
            file_hash=file_hash,
        )

        # Check if this is a .txt or .md file - use simple processing instead of docling
        file_ext = os.path.splitext(file_path)[1].lower()

        if file_ext in (".txt", ".md"):
            if requested_chunking_strategy == "hybrid":
                raise ValueError(
                    "Hybrid chunking was requested but is unavailable for plain-text documents; "
                    "requested_chunking_strategy=hybrid effective_chunking_strategy=none"
                )
            # Simple text file processing without docling
            logger.info(
                "Processing as plain text file (bypassing docling)",
                file_path=file_path,
                file_hash=file_hash,
            )
            slim_doc = process_text_file(file_path)
            slim_doc["parser"] = TEXT_PARSER_LABEL
        else:
            full_doc = await self.docling_service.convert_file(
                file_path,
                user_id=owner_user_id,
                auth_header=jwt_token,
                ocr=ocr,
                picture_descriptions=picture_descriptions,
            )
            slim_doc = extract_relevant(full_doc)
            slim_doc["parser"] = DOCLING_PARSER_LABEL

            if requested_chunking_strategy == "hybrid":
                try:
                    slim_doc["chunks"] = chunk_docling_hybrid(
                        full_doc,
                        max_tokens=hybrid_max_tokens,
                        merge_peers=hybrid_merge_peers,
                    )
                except HybridChunkingError as exc:
                    # The caller records this message on the failed file task.
                    # Include both strategies there as well as on successful
                    # task results: hybrid must never be mistaken for a silent
                    # character-chunking fallback.
                    raise HybridChunkingError(
                        "Hybrid chunking failed; requested_chunking_strategy=hybrid "
                        f"effective_chunking_strategy=none: {exc}"
                    ) from exc
                effective_chunking_strategy = "hybrid"

        # Override filename with original_filename if provided
        if original_filename:
            slim_doc["filename"] = original_filename

        if effective_chunking_strategy != "hybrid" and chunk_size is not None:
            try:
                cs = int(chunk_size)
            except (TypeError, ValueError):
                cs = 0
            if cs > 0:
                try:
                    co = int(chunk_overlap) if chunk_overlap is not None else 0
                except (TypeError, ValueError):
                    co = 0
                if co < cs:
                    slim_doc["chunks"] = resplit_chunks_character_windows(
                        slim_doc["chunks"], cs, max(0, co)
                    )

        # Filter out chunks with empty or whitespace-only text before generating embeddings.
        # This ensures the length of chunks matches the length of the embeddings array,
        # since chunk_texts_for_embeddings also drops empty texts.
        slim_doc["chunks"] = [c for c in slim_doc["chunks"] if c.get("text") and c["text"].strip()]

        litellm_embedding_model = (
            await self.models_service.get_litellm_model_name(embedding_model)
            if self.models_service is not None
            else embedding_model
        )

        litellm_model_lower = litellm_embedding_model.lower() if litellm_embedding_model else ""
        if "watsonx" in litellm_model_lower:
            max_tokens = 500
        elif "ollama" in litellm_model_lower:
            max_tokens = 2000
        else:
            max_tokens = 8000

        # Split any chunks that exceed max_tokens before embedding, ensuring chunks and embeddings align 1-to-1.
        slim_doc["chunks"] = split_chunks_by_max_tokens(
            slim_doc["chunks"], max_tokens, litellm_embedding_model
        )
        # Re-filter out chunks with empty or whitespace-only text that may have resulted from splitting
        slim_doc["chunks"] = [c for c in slim_doc["chunks"] if c.get("text") and c["text"].strip()]
        texts = [c["text"] for c in slim_doc["chunks"]]

        text_batches = chunk_texts_for_embeddings(texts, max_tokens=max_tokens)
        embeddings = []

        for batch in text_batches:
            resp = await clients.patched_embedding_client.embeddings.create(
                model=litellm_embedding_model, input=batch
            )
            embeddings.extend(
                [d["embedding"] if isinstance(d, dict) else d.embedding for d in resp.data]
            )

        if not embeddings or len(embeddings) == 0:
            logger.error(
                "No embeddings generated — document may be empty or unreadable",
                file_hash=file_hash,
                embedding_model=embedding_model,
            )
            return {"status": "error", "error": "No text content could be extracted from document"}

        from services.document_index_writer import (
            DocumentIndexChunk,
            DocumentIndexContext,
            DocumentIndexWriter,
        )

        document_index_writer = getattr(self.document_service, "document_index_writer", None)
        if document_index_writer is None:
            document_index_writer = DocumentIndexWriter()

        # Owner is always the authenticated uploading/syncing user unless shared=True,
        # in which case owner fields are omitted so DLS makes the doc visible to all users.
        owner, owner_name, owner_email = resolve_shared_owner_fields(
            owner_user_id, owner_name, owner_email, shared
        )
        if acl:
            allowed_users = acl.allowed_users or []
            allowed_groups = acl.allowed_groups or []
            allowed_principals = acl.allowed_principals or []
            allowed_principal_labels = acl.allowed_principal_labels or []
        else:
            allowed_users = []
            allowed_groups = []
            allowed_principals = []
            allowed_principal_labels = []

        filename = original_filename if original_filename else slim_doc["filename"]
        index_context = DocumentIndexContext(
            document_id=file_hash,
            filename=filename,
            mimetype=slim_doc["mimetype"],
            embedding_model=embedding_model,
            owner=owner,
            owner_name=owner_name,
            owner_email=owner_email,
            file_size=file_size,
            connector_type=connector_type,
            source_url=source_url,
            source_provenance=source_provenance,
            allowed_users=allowed_users,
            allowed_groups=allowed_groups,
            allowed_principals=allowed_principals,
            allowed_principal_labels=allowed_principal_labels,
            is_sample_data=is_sample_data,
            chunking_strategy=effective_chunking_strategy,
            chunking_config_fingerprint=chunking_fingerprint,
            # Store the new generation under distinct physical ids.  The old
            # generation remains searchable until every new chunk validates.
            ingest_run_id=uuid.uuid4().hex,
        )
        parser_name = slim_doc.get("parser")
        if not parser_name:
            if file_ext in (".txt", ".md"):
                parser_name = TEXT_PARSER_LABEL
            else:
                parser_name = DOCLING_PARSER_LABEL

        chunk_metadata = {
            "parser": parser_name,
            "chunking_strategy": effective_chunking_strategy,
        }
        if chunk_size is not None:
            chunk_metadata["chunk_size"] = chunk_size
        if chunk_overlap is not None:
            chunk_metadata["chunk_overlap"] = chunk_overlap
        if connector_file_id:
            chunk_metadata["connector_file_id"] = connector_file_id

        index_chunks = [
            DocumentIndexChunk(
                chunk_id=f"{file_hash}_{i}",
                text=chunk["text"],
                vector=vect,
                page=chunk["page"],
                chunk_index=i,
                metadata=chunk_metadata,
            )
            for i, (chunk, vect) in enumerate(zip(slim_doc["chunks"], embeddings, strict=True))
        ]
        new_storage_ids = {
            DocumentIndexWriter.storage_chunk_id(index_context, chunk.chunk_id)
            for chunk in index_chunks
        }
        try:
            await document_index_writer.index_chunks(index_context, index_chunks, final=True)
            await self._promote_document_generation(
                opensearch_client=opensearch_client,
                new_storage_ids=new_storage_ids,
                file_hash=file_hash,
                filename=filename,
                owner_user_id=owner_user_id,
                shared=shared,
                replace_existing_filename=replace_existing_filename,
                connector_file_id=connector_file_id if replace_connector_file_id else None,
                connector_type=connector_type,
            )
        except Exception:
            # Promotion is all-or-nothing from the user's perspective: never
            # remove an old generation until this new run is complete, and
            # clean only the temporary run if validation or promotion fails.
            cleanup_writer = (
                document_index_writer
                if hasattr(document_index_writer, "delete_ingest_run")
                else DocumentIndexWriter()
            )
            try:
                await cleanup_writer.delete_ingest_run(
                    index_context.ingest_run_id,
                    index_name=get_index_name(),
                    document_id=file_hash,
                    owner=owner,
                    # No-auth local ingestion intentionally writes ownerless
                    # chunks. Treat that exact ownerless scope as shared for
                    # cleanup; otherwise a failed large ingest leaves its
                    # temporary generation behind indefinitely.
                    shared=shared or owner is None,
                )
            except Exception as cleanup_error:
                logger.error(
                    "Failed to clean temporary document generation after promotion failure",
                    ingest_run_id=index_context.ingest_run_id,
                    error=str(cleanup_error),
                )
            raise
        return {
            "status": "indexed",
            "id": file_hash,
            "requested_chunking_strategy": requested_chunking_strategy,
            "effective_chunking_strategy": effective_chunking_strategy,
        }

    async def process_item(self, upload_task: UploadTask, item: Any, file_task: FileTask) -> None:
        """
        Process a single item in the task.

        This is a base implementation that should be overridden by subclasses.
        When TaskProcessor is used directly (not via subclass), this method
        is not called - only the utility methods like process_document_standard
        are used.

        Args:
            upload_task: The overall upload task
            item: The item to process (could be file path, file info, etc.)
            file_task: The specific file task to update
        """
        raise NotImplementedError(
            "process_item should be overridden by subclasses when used in task processing"
        )


class DocumentFileProcessor(TaskProcessor):
    """Default processor for regular file uploads"""

    def __init__(
        self,
        document_service,
        models_service,
        owner_user_id: str = None,
        jwt_token: str = None,
        owner_name: str = None,
        owner_email: str = None,
        is_sample_data: bool = False,
        connector_type: str = "local",
        docling_service=None,
        replace_duplicates: bool = False,
        session_manager=None,
        settings: dict | None = None,
        source_urls: dict[str, str] | None = None,
        source_provenances: dict[str, "SourceProvenance"] | None = None,
        archive_sources: bool = False,
        delete_source_after_success: bool = False,
    ):
        super().__init__(
            document_service,
            models_service,
            docling_service=docling_service
            or (document_service.docling_service if document_service else None),
        )
        self.owner_user_id = owner_user_id
        self.jwt_token = jwt_token
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.is_sample_data = is_sample_data
        self.connector_type = connector_type
        self.replace_duplicates = replace_duplicates
        self.session_manager = session_manager or (
            document_service.session_manager if document_service else None
        )
        self.settings = settings
        self.source_urls = source_urls or {}
        self.source_provenances = source_provenances or {}
        self.archive_sources = archive_sources
        self.delete_source_after_success = delete_source_after_success
        if self.session_manager is None:
            raise ValueError("session_manager is required for DocumentFileProcessor")

    async def process_item(self, upload_task: UploadTask, item: str, file_task: FileTask) -> None:
        """Process a regular file path using consolidated methods"""
        file_task.status = TaskStatus.RUNNING
        file_task.updated_at = time.time()

        staged_source = None
        archive_committed = False
        try:
            # Use the ORIGINAL filename stored in file_task (not the transformed temp path)
            # This ensures we check/store the original filename with spaces, etc.
            original_filename = file_task.filename or os.path.basename(item)

            # Check if document with same filename already exists
            if self.session_manager is None:
                raise ValueError("session_manager is required to get OpenSearch client")
            opensearch_client = self.session_manager.get_user_opensearch_client(
                self.owner_user_id, self.jwt_token
            )

            # Path ingestion identifies known content by its hash inside
            # process_document_standard. A matching filename alone must not
            # discard a different document from the shared ingestion folder.
            duplicate_action = "proceed"
            if not self.delete_source_after_success:
                duplicate_action = await self.resolve_duplicate_filename(
                    original_filename,
                    opensearch_client,
                    replace=self.replace_duplicates,
                    owner_user_id=self.owner_user_id,
                )
                if duplicate_action == "skip":
                    self.mark_duplicate_skipped(upload_task, file_task)
                    return

            # Compute hash
            file_hash = hash_id(item)
            # Chunks are indexed with document_id=file_hash (see
            # process_document_standard -> DocumentIndexContext), so record it on
            # the file_task for preview-mode index proof lookups.
            file_task.document_id = file_hash

            source_url = self.source_urls.get(str(item))
            source_provenance = self.source_provenances.get(str(item))
            processing_path = item
            if self.archive_sources:
                from services.local_source_service import local_source_url, stage_local_source

                staged_source = await stage_local_source(item, file_hash, original_filename)
                processing_path = str(staged_source.archived_path)
                source_url = source_url or local_source_url(staged_source.source_id)

            # Get file size
            try:
                file_size = os.path.getsize(processing_path)
            except Exception:
                file_size = 0

            # Parse ACL from settings if present
            from connectors.base import DocumentACL

            acl = None
            if self.settings and (
                self.settings.get("allowed_users") is not None
                or self.settings.get("allowed_groups") is not None
            ):
                acl = DocumentACL(
                    owner=self.owner_user_id,
                    allowed_users=self.settings.get("allowed_users", []),
                    allowed_groups=self.settings.get("allowed_groups", []),
                )

            standard_kwargs: dict[str, Any] = {}
            if self.settings:
                s = self.settings
                em = s.get("embeddingModel")
                if isinstance(em, str) and em.strip():
                    standard_kwargs["embedding_model"] = em.strip()
                for ui_key, param in (
                    ("chunkSize", "chunk_size"),
                    ("chunkOverlap", "chunk_overlap"),
                ):
                    raw = s.get(ui_key)
                    if raw is not None:
                        try:
                            standard_kwargs[param] = int(raw)
                        except (TypeError, ValueError):
                            pass

            config = get_openrag_config()
            standard_kwargs["ocr"] = config.knowledge.ocr
            standard_kwargs["picture_descriptions"] = config.knowledge.picture_descriptions

            # Use consolidated standard processing
            result = await self.process_document_standard(
                file_path=processing_path,
                file_hash=file_hash,
                owner_user_id=self.owner_user_id,
                original_filename=original_filename,
                jwt_token=self.jwt_token,
                owner_name=self.owner_name,
                owner_email=self.owner_email,
                file_size=file_size,
                connector_type=self.connector_type,
                is_sample_data=self.is_sample_data,
                acl=acl,
                source_url=source_url,
                source_provenance=source_provenance,
                replace_existing_filename=duplicate_action == "replace_pending",
                **standard_kwargs,
            )

            if result.get("status") == "error":
                file_task.status = TaskStatus.FAILED
                file_task.error = result.get("error") or "Failed to process document"
                file_task.updated_at = time.time()
                upload_task.failed_files += 1
            else:
                result_status = result.get("status")
                # ``unchanged`` means the content hash was already present and
                # no chunk was rewritten with this new source URL. Path ingest
                # consumes that redundant input; other callers retain it.
                if result_status == "indexed":
                    if staged_source is not None:
                        staged_source.commit()
                        archive_committed = True
                    elif self.delete_source_after_success:
                        from services.local_source_service import delete_ingested_source

                        deleted = delete_ingested_source(item)
                        if not deleted and os.path.exists(item):
                            logger.warning(
                                "Failed to remove successfully ingested local source",
                                file_path=item,
                            )
                elif result_status == "unchanged" and self.delete_source_after_success:
                    if staged_source is not None:
                        if await staged_source.discard():
                            staged_source = None
                        else:
                            logger.warning(
                                "Failed to discard unchanged staged source",
                                file_path=item,
                            )
                    else:
                        from services.local_source_service import delete_ingested_source

                        deleted = delete_ingested_source(item)
                        if not deleted and os.path.exists(item):
                            logger.warning(
                                "Failed to remove unchanged local source",
                                file_path=item,
                            )
                file_task.status = TaskStatus.COMPLETED
                file_task.result = result
                file_task.updated_at = time.time()
                upload_task.successful_files += 1

        except Exception as e:
            file_task.status = TaskStatus.FAILED
            file_task.error = str(e) or repr(e)
            file_task.updated_at = time.time()
            upload_task.failed_files += 1
            raise
        finally:
            if staged_source is not None and not archive_committed:
                await staged_source.rollback()


class ConnectorFileProcessor(TaskProcessor):
    """Processor for connector file uploads"""

    def __init__(
        self,
        connector_service,
        connection_id: str,
        files_to_process: list,
        user_id: str = None,
        jwt_token: str = None,
        owner_name: str = None,
        owner_email: str = None,
        document_service=None,
        models_service=None,
        ingest_settings: dict[str, Any] | None = None,
        replace_duplicates: bool = False,
        connector_type: str | None = None,
        shared: bool = False,
    ):
        super().__init__(
            document_service=document_service,
            models_service=models_service,
            docling_service=document_service.docling_service if document_service else None,
        )
        self.connector_service = connector_service
        self.connection_id = connection_id
        self.files_to_process = files_to_process
        self.user_id = user_id
        self.jwt_token = jwt_token
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.ingest_settings = ingest_settings
        self.replace_duplicates = replace_duplicates
        self.connector_type = connector_type
        self.shared = shared

    async def _reconcile_shared_owner(self, filename: str) -> None:
        """Update owner fields on already-indexed chunks for `filename` to match
        the connector's current `shared` setting.

        Called on the duplicate/unchanged skip paths below, where a file's
        content and name haven't changed since a prior sync but the connector's
        "Make documents available to all users" setting may have been toggled
        since then. Without this, those chunks would keep whatever owner they
        got on their original ingest forever, since a byte-identical re-sync
        never reaches resolve_shared_owner_fields(). Scoped to chunks owned by
        this user or already ownerless (matching the same boundary
        delete_document_by_filename uses), so it can't touch another user's
        private document that happens to share this filename.
        """
        write_client = clients.opensearch
        if write_client is None:
            return
        owner, owner_name, owner_email = resolve_shared_owner_fields(
            self.user_id, self.owner_name, self.owner_email, self.shared
        )
        for candidate in get_filename_aliases(filename):
            try:
                await write_client.update_by_query(
                    index=get_index_name(),
                    body={
                        "query": build_replace_filename_query(candidate, self.user_id),
                        "script": {
                            "source": """
                                if (params.shared) {
                                    ctx._source.remove('owner');
                                } else {
                                    ctx._source.owner = params.owner;
                                }
                                ctx._source.owner_name = params.owner_name;
                                ctx._source.owner_email = params.owner_email;
                            """,
                            "params": {
                                "shared": self.shared,
                                "owner": owner,
                                "owner_name": owner_name,
                                "owner_email": owner_email,
                            },
                        },
                    },
                )
            except Exception as e:
                logger.warning(
                    "Failed to reconcile owner fields for skipped duplicate",
                    filename=candidate,
                    error=str(e),
                )

    async def process_item(self, upload_task: UploadTask, item: str, file_task: FileTask) -> None:
        """Process a connector file using unified methods"""
        file_task.status = TaskStatus.RUNNING
        file_task.updated_at = time.time()

        try:
            file_id = item  # item is the connector file ID

            # Get the connector and connection info
            connector = await self.connector_service.get_connector(self.connection_id)
            connection = await self.connector_service.connection_manager.get_connection(
                self.connection_id
            )
            if not connector or not connection:
                raise ValueError(f"Connection '{self.connection_id}' not found")

            connector_type = self.connector_type or connection.connector_type
            file_task.connector_type = connector_type

            # Validate file extension early if filename is available
            VALID_EXTENSIONS = {
                "adoc",
                "asciidoc",
                "asc",
                "bmp",
                "csv",
                "dotx",
                "dotm",
                "docm",
                "docx",
                "htm",
                "html",
                "jpeg",
                "jpg",
                "md",
                "pdf",
                "png",
                "potx",
                "ppsx",
                "pptm",
                "potm",
                "ppsm",
                "pptx",
                "tiff",
                "txt",
                "xls",
                "xlsx",
                "xhtml",
                "webp",
            }
            # Only pre-validate when we have a real filename. When the filename
            # falls back to the connector file_id (e.g. a deletion event re-added
            # by sync_specific_files, where no name is known), skip this check so
            # the deletion reaches the 404 -> chunk-cleanup path below. Files that
            # still exist are re-validated after download (see below).
            if file_task.filename and file_task.filename != file_id:
                ext = file_task.filename.split(".")[-1].lower() if "." in file_task.filename else ""
                if ext not in VALID_EXTENSIONS:
                    file_task.status = TaskStatus.FAILED
                    file_task.error = f"The file '{file_task.filename}' has an incompatible type."
                    file_task.updated_at = time.time()
                    upload_task.failed_files += 1
                    return

            # Get file content from connector
            try:
                document = await connector.get_file_content(file_id)
            except (FileNotFoundError, ValueError) as e:
                msg = str(e).lower()
                if "not found" in msg or "404" in msg:
                    # File gone at source — remove its indexed chunks by the
                    # stable connector id (matches both connector_file_id and
                    # document_id) so it stops appearing in search/chat.
                    opensearch_client = (
                        self.document_service.session_manager.get_user_opensearch_client(
                            self.user_id, self.jwt_token
                        )
                    )
                    deleted_chunks = await self._delete_connector_chunks(
                        file_id, opensearch_client, self.user_id, shared=self.shared
                    )

                    logger.warning(
                        "File no longer exists at source — removed from index",
                        file_id=file_id,
                        connection_id=self.connection_id,
                        deleted_chunks=deleted_chunks,
                        error=str(e),
                    )
                    file_task.status = TaskStatus.SKIPPED
                    file_task.result = {
                        "status": "skipped",
                        "reason": "deleted_at_source",
                        "deleted_chunks": deleted_chunks,
                        # Human-readable message so the tasks view shows this
                        # successful cleanup instead of falling back to
                        # "Unknown error" for a skip with no message.
                        "warning": (
                            f"File no longer exists at source; removed from index "
                            f"({deleted_chunks} chunk(s) deleted)."
                        ),
                    }
                    file_task.updated_at = time.time()
                    upload_task.successful_files += 1
                    return
                raise

            # Update filename in task once we have it from the connector
            file_task.filename = clean_connector_filename(document.filename, document.mimetype)

            # Re-check filename validation
            name = file_task.filename or document.filename or ""
            ext = name.split(".")[-1].lower() if "." in name else ""
            if ext not in VALID_EXTENSIONS:
                file_task.status = TaskStatus.FAILED
                file_task.error = f"The file '{name}' has an incompatible type."
                file_task.updated_at = time.time()
                upload_task.failed_files += 1
                return

            if not self.user_id:
                raise ValueError("user_id not provided to ConnectorFileProcessor")

            opensearch_client = self.document_service.session_manager.get_user_opensearch_client(
                self.user_id, self.jwt_token
            )

            duplicate_action = await self.resolve_duplicate_filename(
                file_task.filename,
                opensearch_client,
                replace=self.replace_duplicates,
                owner_user_id=self.user_id,
                shared=self.shared,
            )
            if duplicate_action == "skip":
                await self._reconcile_shared_owner(file_task.filename)
                self.mark_duplicate_skipped(upload_task, file_task)
                return

            config = get_openrag_config()
            chunking_strategy = _require_chunking_strategy(config)
            knowledge_config = config.knowledge

            # Rename cleanup: a connector file keeps a stable id across renames,
            # but chunks are keyed by filename/content-hash, so a renamed file
            # leaves its OLD-name chunks orphaned. Drop chunks for this id whose
            # filename differs from the current one. If any were removed (a real
            # rename), force a re-ingest below so the file is re-indexed under
            # the new name instead of short-circuiting as "unchanged".
            # Match against file_task.filename — the cleaned name the file is
            # actually indexed under — so duplicate/rename detection lines up
            # with how chunks are keyed.
            if chunking_strategy == "hybrid":
                # Hybrid replacement is transactional: detect a rename without
                # deleting its old chunks. The backend promotes the validated
                # new generation and only then removes these stale ids.
                from connectors.chunk_cleanup import build_connector_file_chunks_query
                from utils.opensearch_delete import collect_visible_document_ids

                renamed = bool(
                    await collect_visible_document_ids(
                        opensearch_client,
                        index=get_index_name(),
                        query=build_connector_file_chunks_query(
                            [document.id],
                            connector_type=connector_type,
                            owner_user_id=self.user_id,
                            shared=self.shared,
                            keep_filenames=get_filename_aliases(file_task.filename),
                        ),
                    )
                )
            else:
                renamed = (
                    await self._delete_connector_chunks(
                        document.id,
                        opensearch_client,
                        self.user_id,
                        keep_filenames=get_filename_aliases(file_task.filename),
                        shared=self.shared,
                        connector_type=connector_type,
                    )
                    > 0
                )

            # Create temporary file from document content
            suffix = os.path.splitext(file_task.filename)[1]
            if not suffix:
                suffix = get_file_extension(document.mimetype)
            with auto_cleanup_tempfile(suffix=suffix) as tmp_path:
                # Write content to temp file
                with open(tmp_path, "wb") as f:
                    f.write(document.content)

                # Compute hash
                file_hash = hash_id(tmp_path)

                if (
                    chunking_strategy != "hybrid"
                    and not renamed
                    and await self.check_document_exists(
                        file_hash,
                        opensearch_client,
                        owner_user_id=self.user_id,
                        shared=self.shared,
                    )
                ):
                    await self._reconcile_shared_owner(file_task.filename)
                    file_task.status = TaskStatus.COMPLETED
                    file_task.result = {"status": "unchanged", "id": file_hash}
                    file_task.updated_at = time.time()
                    upload_task.successful_files += 1
                    return

                from config.settings import DISABLE_INGEST_WITH_LANGFLOW

                if (
                    not knowledge_config.disable_ingest_with_langflow
                    and not DISABLE_INGEST_WITH_LANGFLOW
                    # HybridChunker is implemented by the backend-owned
                    # standard processor.  Do not silently send connectors
                    # through Langflow's independent chunking path.
                    and chunking_strategy != "hybrid"
                    and self.connector_service.langflow_service is not None
                ):
                    # Delete existing chunks for this document before Langflow re-ingestion
                    try:
                        from utils.opensearch_delete import (
                            collect_visible_document_ids,
                            delete_document_ids,
                        )

                        # Match both fields: bucket-connector chunks carry the
                        # raw connector id in connector_file_id (document_id is
                        # a hash), while pre-migration chunks only have it in
                        # document_id.
                        chunk_ids = await collect_visible_document_ids(
                            opensearch_client,
                            index=get_index_name(),
                            query={
                                "bool": {
                                    "filter": [
                                        (
                                            {"bool": {"must_not": {"exists": {"field": "owner"}}}}
                                            if self.shared
                                            else {"term": {"owner": self.user_id}}
                                        )
                                    ],
                                    "should": [
                                        {"term": {"document_id": document.id}},
                                        {"term": {"connector_file_id": document.id}},
                                        # See check_document_exists: some indices
                                        # predate the explicit keyword mapping for
                                        # this field.
                                        {"term": {"connector_file_id.keyword": document.id}},
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        )
                        deleted_count = await delete_document_ids(
                            opensearch_client,
                            index=get_index_name(),
                            document_ids=chunk_ids,
                            refresh=True,
                        )
                        logger.info(
                            "Deleted existing chunks before Langflow re-ingestion",
                            document_id=document.id,
                            deleted_count=deleted_count,
                        )
                    except Exception as delete_err:
                        logger.warning(
                            "Failed to delete existing chunks before Langflow re-ingestion",
                            document_id=document.id,
                            error=str(delete_err),
                        )

                    # Ingest via unified Langflow pipeline (two-phase Docling + Langflow run)
                    langflow_filename, processed_mimetype = langflow_safe_filename_and_mimetype(
                        file_task.filename, document.mimetype
                    )
                    file_tuple = (langflow_filename, document.content, processed_mimetype)

                    # Extract ACL information
                    allowed_users: list[str] = []
                    allowed_groups: list[str] = []
                    allowed_principals: list[str] = []
                    allowed_principal_labels: list[dict[str, Any]] = []
                    if document.acl:
                        try:
                            allowed_users = document.acl.allowed_users or []
                            allowed_groups = document.acl.allowed_groups or []
                            allowed_principals = document.acl.allowed_principals or []
                            allowed_principal_labels = document.acl.allowed_principal_labels or []
                        except AttributeError:
                            pass

                    # Prepare tweaks
                    connector_tweak_settings = None
                    if isinstance(self.ingest_settings, dict):
                        connector_tweak_settings = dict(self.ingest_settings)
                        connector_tweak_settings.pop("embeddingModel", None)

                    tweaks = self.connector_service.langflow_service.merge_ui_ingest_settings_into_tweaks(
                        {}, connector_tweak_settings
                    )

                    config = get_openrag_config()
                    effective_ingest_settings = (
                        dict(self.ingest_settings) if self.ingest_settings else {}
                    )
                    effective_ingest_settings["ocr"] = config.knowledge.ocr
                    effective_ingest_settings["pictureDescriptions"] = (
                        config.knowledge.picture_descriptions
                    )

                    effective_owner, effective_owner_name, effective_owner_email = (
                        resolve_shared_owner_fields(
                            self.user_id, self.owner_name, self.owner_email, self.shared
                        )
                    )
                    file_task.document_id = document.id
                    result = await self.connector_service.langflow_service.upload_and_ingest_file(
                        file_tuple=file_tuple,
                        session_id=None,
                        tweaks=tweaks,
                        settings=effective_ingest_settings,
                        jwt_token=self.jwt_token,
                        owner=effective_owner,
                        owner_name=effective_owner_name,
                        owner_email=effective_owner_email,
                        connector_type=connector_type,
                        docling_polling_service=self.connector_service.task_service.docling_polling_service
                        if self.connector_service.task_service
                        else None,
                        file_task=file_task,
                        connector_file_id=document.id,
                        source_url=document.source_url,
                        allowed_users=allowed_users,
                        allowed_groups=allowed_groups,
                        allowed_principals=allowed_principals,
                        allowed_principal_labels=allowed_principal_labels,
                        original_filename=file_task.filename,
                        original_mimetype=document.mimetype,
                    )
                    # Langflow returns "success" even when no text was extracted
                    # (e.g. image files without OCR). Verify the document actually
                    # landed in OpenSearch before declaring success.
                    # wait_for_visibility polls on an empty result to ride out
                    # OpenSearch's ~1s near-real-time window (the user-scoped
                    # client cannot force an indices:admin/refresh — it 403s).
                    if not await self.check_document_exists(
                        document.id,
                        _verification_client(opensearch_client),
                        on_error="assume_exists",
                        wait_for_visibility=True,
                        field="connector_file_id",
                        owner_user_id=self.user_id,
                        shared=self.shared,
                    ):
                        result = {
                            "status": "error",
                            "error": "No text content could be extracted from document",
                        }

                    # Persist connector metadata (incl. modified_time) onto the
                    # Langflow-indexed chunks (keyed by document_id) so bucket-connector
                    # change detection has a stored timestamp to compare against on the
                    # next sync. Mirrors the standard path's enrichment below.
                    if result.get("status") != "error":
                        await self.connector_service._update_connector_metadata(
                            document,
                            self.user_id,
                            connector_type,
                            self.jwt_token,
                            indexed_filename=file_task.filename,
                        )
                else:
                    # Standard OpenRAG processing pipeline (process_document_standard)
                    standard_kwargs: dict[str, Any] = {}
                    if isinstance(self.ingest_settings, dict):
                        s = self.ingest_settings
                        em = s.get("embeddingModel")
                        if isinstance(em, str) and em.strip():
                            standard_kwargs["embedding_model"] = em.strip()
                        for ui_key, param in (
                            ("chunkSize", "chunk_size"),
                            ("chunkOverlap", "chunk_overlap"),
                        ):
                            raw = s.get(ui_key)
                            if raw is not None:
                                try:
                                    standard_kwargs[param] = int(raw)
                                except (TypeError, ValueError):
                                    pass
                    config = get_openrag_config()
                    standard_kwargs["ocr"] = config.knowledge.ocr
                    standard_kwargs["picture_descriptions"] = config.knowledge.picture_descriptions

                    result = await self.process_document_standard(
                        file_path=tmp_path,
                        file_hash=file_hash,
                        owner_user_id=self.user_id,
                        original_filename=file_task.filename,
                        jwt_token=self.jwt_token,
                        owner_name=self.owner_name,
                        owner_email=self.owner_email,
                        file_size=len(document.content),
                        connector_type=connector_type,
                        acl=document.acl,
                        connector_file_id=document.id,
                        shared=self.shared,
                        replace_existing_filename=duplicate_action == "replace_pending",
                        replace_connector_file_id=renamed,
                        force_reprocess=renamed,
                        **standard_kwargs,
                    )

                    # Update indexed chunks with connector-specific metadata
                    if result["status"] in ["indexed", "unchanged"]:
                        await self.connector_service._update_connector_metadata(
                            document,
                            self.user_id,
                            connector_type,
                            self.jwt_token,
                            indexed_filename=file_task.filename,
                        )

                    # Add connector-specific metadata
                    result.update(
                        {
                            "source_url": document.source_url,
                            "document_id": document.id,
                        }
                    )

            if result.get("status") == "error":
                file_task.status = TaskStatus.FAILED
                file_task.error = result.get("error") or "Failed to process document"
                file_task.updated_at = time.time()
                upload_task.failed_files += 1
            else:
                file_task.status = TaskStatus.COMPLETED
                file_task.result = result
                file_task.updated_at = time.time()
                upload_task.successful_files += 1

        except Exception as e:
            file_task.status = TaskStatus.FAILED
            file_task.error = str(e) or repr(e)
            file_task.updated_at = time.time()
            upload_task.failed_files += 1
            raise


class S3FileProcessor(TaskProcessor):
    """Processor for files stored in S3 buckets"""

    def __init__(
        self,
        document_service,
        bucket: str,
        s3_client=None,
        owner_user_id: str = None,
        jwt_token: str = None,
        owner_name: str = None,
        owner_email: str = None,
        models_service=None,
        docling_service=None,
        replace_duplicates: bool = False,
    ):
        import boto3

        super().__init__(
            document_service,
            models_service,
            docling_service,
        )
        self.bucket = bucket
        self.s3_client = s3_client or boto3.client("s3")
        self.owner_user_id = owner_user_id
        self.jwt_token = jwt_token
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.replace_duplicates = replace_duplicates

    async def process_item(self, upload_task: UploadTask, item: str, file_task: FileTask) -> None:
        """Download an S3 object and process it using DocumentService"""
        import time

        from models.tasks import TaskStatus

        file_task.status = TaskStatus.RUNNING
        file_task.updated_at = time.time()

        try:
            # The S3 key is also the indexed filename, so duplicate policy can
            # be resolved before downloading the object.
            opensearch_client = self.document_service.session_manager.get_user_opensearch_client(
                self.owner_user_id, self.jwt_token
            )
            duplicate_action = await self.resolve_duplicate_filename(
                item,
                opensearch_client,
                replace=self.replace_duplicates,
                owner_user_id=self.owner_user_id,
            )
            if duplicate_action == "skip":
                self.mark_duplicate_skipped(upload_task, file_task)
                return

            suffix = os.path.splitext(item)[1]
            with auto_cleanup_tempfile(suffix=suffix) as tmp_path:
                # Download object to temporary file
                with open(tmp_path, "wb") as tmp_file:
                    self.s3_client.download_fileobj(self.bucket, item, tmp_file)

                # Compute hash
                file_hash = hash_id(tmp_path)

                # Get object size
                try:
                    obj_info = self.s3_client.head_object(Bucket=self.bucket, Key=item)
                    file_size = obj_info.get("ContentLength", 0)
                except Exception:
                    file_size = 0

                # Use consolidated standard processing
                result = await self.process_document_standard(
                    file_path=tmp_path,
                    file_hash=file_hash,
                    owner_user_id=self.owner_user_id,
                    original_filename=item,  # Use S3 key as filename
                    jwt_token=self.jwt_token,
                    owner_name=self.owner_name,
                    owner_email=self.owner_email,
                    file_size=file_size,
                    connector_type="s3",
                    replace_existing_filename=duplicate_action == "replace_pending",
                )

                result["path"] = f"s3://{self.bucket}/{item}"
                if result.get("status") == "error":
                    file_task.status = TaskStatus.FAILED
                    file_task.error = result.get("error") or "Failed to process document"
                    upload_task.failed_files += 1
                else:
                    file_task.status = TaskStatus.COMPLETED
                    file_task.result = result
                    upload_task.successful_files += 1

        except Exception as e:
            file_task.status = TaskStatus.FAILED
            file_task.error = str(e) or repr(e)
            upload_task.failed_files += 1
        finally:
            file_task.updated_at = time.time()


class LangflowFileProcessor(TaskProcessor):
    """Processor for Langflow file uploads with two-phase Docling + Langflow ingestion."""

    def __init__(
        self,
        langflow_file_service,
        session_manager,
        owner_user_id: str = None,
        jwt_token: str = None,
        owner_name: str = None,
        owner_email: str = None,
        session_id: str = None,
        tweaks: dict = None,
        settings: dict = None,
        replace_duplicates: bool = False,
        connector_type: str = "local",
        docling_polling_service=None,
        source_urls: dict[str, str] | None = None,
        source_provenances: dict[str, "SourceProvenance"] | None = None,
        archive_sources: bool = False,
    ):
        super().__init__()
        self.langflow_file_service = langflow_file_service
        self.session_manager = session_manager
        self.owner_user_id = owner_user_id
        self.jwt_token = jwt_token
        self.owner_name = owner_name
        self.owner_email = owner_email
        self.session_id = session_id
        self.tweaks = tweaks or {}
        self.settings = settings
        self.replace_duplicates = replace_duplicates
        self.connector_type = connector_type
        self.docling_polling_service = docling_polling_service
        self.source_urls = source_urls or {}
        self.source_provenances = source_provenances or {}
        self.archive_sources = archive_sources

    async def process_item(self, upload_task: UploadTask, item: str, file_task: FileTask) -> None:
        """Process a file path using LangflowFileService upload_and_ingest_file"""
        # Update task status
        file_task.status = TaskStatus.RUNNING
        file_task.updated_at = time.time()

        staged_source = None
        archive_committed = False
        try:
            # Use the ORIGINAL filename stored in file_task (not the transformed temp path)
            # This ensures we check/store the original filename with spaces, etc.
            original_filename = file_task.filename or os.path.basename(item)

            # Check if document with same filename already exists
            opensearch_client = self.session_manager.get_user_opensearch_client(
                self.owner_user_id, self.jwt_token
            )

            duplicate_action = await self.resolve_duplicate_filename(
                original_filename,
                opensearch_client,
                replace=self.replace_duplicates,
                owner_user_id=self.owner_user_id,
            )
            if duplicate_action == "skip":
                self.mark_duplicate_skipped(upload_task, file_task)
                return

            # Read file content for processing
            with open(item, "rb") as f:
                content = f.read()

            # Create file tuple for upload using ORIGINAL filename
            # This ensures the document is indexed with the original name
            original_mimetype, _ = mimetypes.guess_type(original_filename)
            if not original_mimetype:
                original_mimetype = "application/octet-stream"

            # Langflow's docling chokes on text/plain — rename .txt -> .md.
            langflow_filename, content_type = langflow_safe_filename_and_mimetype(
                original_filename, original_mimetype
            )
            file_tuple = (langflow_filename, content, content_type)

            effective_jwt = self.jwt_token
            if self.session_manager and not effective_jwt:
                effective_jwt = self.session_manager.get_effective_jwt_token(
                    self.owner_user_id,
                    None,
                )

            # Prepare metadata tweaks similar to API endpoint
            final_tweaks = self.tweaks.copy() if self.tweaks else {}

            file_hash = hash_id(item)
            file_task.document_id = file_hash

            source_url = self.source_urls.get(str(item))
            source_provenance = self.source_provenances.get(str(item))
            if self.archive_sources:
                from services.local_source_service import local_source_url, stage_local_source

                staged_source = await stage_local_source(item, file_hash, original_filename)
                source_url = source_url or local_source_url(staged_source.source_id)

            # Build settings with fresh OCR/pictureDescriptions from live
            # config so retries pick up configuration changes.
            config = get_openrag_config()
            effective_settings = dict(self.settings) if self.settings else {}
            effective_settings["ocr"] = config.knowledge.ocr
            effective_settings["pictureDescriptions"] = config.knowledge.picture_descriptions

            # Process file using langflow service. Passing the polling
            # service triggers the two-phase model: backend polls Docling,
            # then invokes Langflow only after SUCCESS. file_task is passed
            # so phase / docling_status are tracked on the task record.
            result = await self.langflow_file_service.upload_and_ingest_file(
                file_tuple=file_tuple,
                session_id=self.session_id,
                tweaks=final_tweaks,
                settings=effective_settings,
                jwt_token=effective_jwt,
                owner=self.owner_user_id,
                owner_name=self.owner_name,
                owner_email=self.owner_email,
                connector_type=self.connector_type,
                docling_polling_service=self.docling_polling_service,
                file_task=file_task,
                document_id=file_hash,
                source_url=source_url,
                source_provenance=source_provenance,
                original_filename=original_filename,
                original_mimetype=original_mimetype,
            )

            # Langflow returns "success" even when no text was extracted
            # (e.g. image files without OCR). Verify the document actually
            # landed in OpenSearch before declaring success. We key off the
            # filename — the identifier this path already uses for dedup and
            # delete (see check_filename_exists / delete_document_by_filename
            # above). The document_id (hash_id(item) == content hash) is now
            # threaded through to Langflow so preview-mode index proof can look
            # chunks up by document_id, but verification stays filename-based to
            # match this path's existing dedup/delete semantics.
            #
            # wait_for_visibility polls on an empty result so the just-written
            # chunks become visible within OpenSearch's near-real-time refresh
            # window. We cannot force a refresh here: the user-scoped client
            # lacks the indices:admin/refresh privilege (it 403s).
            if not await self.check_filename_exists(
                original_filename,
                _verification_client(opensearch_client),
                wait_for_visibility=True,
            ):
                file_task.status = TaskStatus.FAILED
                file_task.error = "No text content could be extracted from document"
                file_task.updated_at = time.time()
                upload_task.failed_files += 1
            else:
                # Update task with success
                if staged_source is not None:
                    staged_source.commit()
                    archive_committed = True
                file_task.status = TaskStatus.COMPLETED
                file_task.result = result
                file_task.updated_at = time.time()
                upload_task.successful_files += 1

        except Exception as e:
            # Update task with failure
            file_task.status = TaskStatus.FAILED
            file_task.error = str(e) or repr(e)
            file_task.updated_at = time.time()
            upload_task.failed_files += 1
            raise
        finally:
            if staged_source is not None and not archive_committed:
                await staged_source.rollback()
