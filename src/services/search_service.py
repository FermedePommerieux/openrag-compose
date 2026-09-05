import asyncio
import copy
import hashlib
import json
import os
import re
import time
from typing import Any

from agentd.tool_decorator import tool

from auth_context import get_auth_context
from config.embedding_constants import get_declared_default_embedding_model
from config.settings import clients, get_embedding_model, get_index_name, get_openrag_config
from models.metadata_filter import MetadataFilter
from models.source_provenance import validate_provenance_representative
from models.structured_document_query import MetadataCandidateRestriction
from services.metadata_candidate_restriction import (
    execute_metadata_restricted_lane,
    resolve_metadata_candidates,
)
from services.model_capabilities import (
    build_responses_request,
    model_capability_profile,
    resolve_planner_selection,
)
from services.opensearch_response import validate_search_progress, validate_search_response
from services.retrieval_service import (
    EXHAUSTIVE_BATCH_MAX,
    EXHAUSTIVE_PROFILE_VERSION,
    MAX_DISCOVERY_QUERIES,
    DiscoveryQuery,
    HttpReranker,
    RetrievalSettings,
    ScopeCertificationFacts,
    ScopeExhaustiveSettings,
    build_discovery_plan,
    certify_scope_coverage,
    decode_exhaustive_cursor,
    discovery_plan_audit,
    discovery_query_prompt,
    document_manifest_sha256,
    encode_exhaustive_cursor,
    exhaustive_scope_sha256,
    expand_provenance_graph,
    hit_identity,
    limit_chunks_per_document,
    multi_query_reciprocal_rank_fusion,
    reciprocal_rank_fusion,
    requested_retrieval_profile,
    retrieval_execution_complete,
    verified_chunk_manifest,
    verify_complete_document,
)
from services.scope_traversal_policy import DEFAULT_SCOPE_TRAVERSAL_POLICY
from utils.container_utils import transform_localhost_url
from utils.logging_config import get_logger
from utils.rrf_mapping import RRFMappingError, require_sortable_chunk_id_mapping

logger = get_logger(__name__)

MAX_EMBED_RETRIES = 3
EMBED_RETRY_INITIAL_DELAY = 1.0
EMBED_RETRY_MAX_DELAY = 8.0
DOCUMENT_SEARCH_RESULT_WINDOW = 10_000

_RETRIEVAL_LANE_FAILURE_CODES = {
    "lexical": "retrieval_lexical_lane_failed",
    "dense": "retrieval_dense_lane_failed",
    "fusion": "retrieval_fusion_failed",
    "multi_query": "retrieval_execution_incomplete",
}


def _initial_effective_retrieval_profile(requested: dict[str, Any]) -> dict[str, Any]:
    lanes: dict[str, dict[str, Any]] = {}
    for lane, requirement in requested.get("lanes", {}).items():
        lanes[lane] = {
            "requested": requirement == "required",
            "status": "failed" if requirement == "required" else "not_requested",
            "candidates": 0,
        }
        if requirement == "required":
            lanes[lane]["error"] = "not_executed"
    return {
        "version": requested.get("version", 1),
        "strategy": requested.get("strategy"),
        "mode": "none",
        "lanes": lanes,
    }


def _finalize_retrieval_contract(
    response: dict[str, Any],
    *,
    requested: dict[str, Any],
    effective: dict[str, Any],
    failure_codes: list[str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach one fail-closed, machine-readable retrieval execution contract."""

    lanes = effective.get("lanes", {})
    lexical_succeeded = lanes.get("lexical", {}).get("status") == "succeeded"
    dense_succeeded = lanes.get("dense", {}).get("status") == "succeeded"
    if lexical_succeeded and dense_succeeded:
        effective["mode"] = "hybrid"
    elif dense_succeeded:
        effective["mode"] = "vector"
    elif lexical_succeeded:
        effective["mode"] = "lexical"
    else:
        effective["mode"] = "none"

    resolved_failure_codes = list(failure_codes or [])
    for lane, requirement in requested.get("lanes", {}).items():
        if requirement != "required":
            continue
        if lanes.get(lane, {}).get("status") != "succeeded":
            if lane == "multi_query" and any(
                code in {"multi_query_planner_failed", "multi_query_query_failed"}
                for code in resolved_failure_codes
            ):
                continue
            resolved_failure_codes.append(_RETRIEVAL_LANE_FAILURE_CODES[lane])
    resolved_failure_codes = list(dict.fromkeys(resolved_failure_codes))
    complete = retrieval_execution_complete(requested, effective)
    if not complete and not resolved_failure_codes:
        resolved_failure_codes.append("retrieval_execution_incomplete")

    response["requested_retrieval_profile"] = requested
    response["effective_retrieval_profile"] = effective
    response["retrieval_execution_complete"] = complete
    response["retrieval_failure_codes"] = resolved_failure_codes
    combined_warnings = [value for value in response.get("warnings", []) if isinstance(value, dict)]
    combined_warnings.extend(value for value in (warnings or []) if isinstance(value, dict))
    if combined_warnings:
        response["warnings"] = combined_warnings
    return response


# Variable used to store the active instance for the tool wrapper
_global_search_service = None


def _build_file_facet_aggregations() -> dict[str, Any]:
    """Build the file-level facets shared by weighted and RRF search lanes."""
    return {
        "data_sources": {"terms": {"field": "filename", "size": 20}},
        "document_types": {"terms": {"field": "mimetype", "size": 10}},
        "owners": {"terms": {"field": "owner", "size": 10}},
        "connector_types": {"terms": {"field": "connector_type", "size": 10}},
        "embedding_models": {"terms": {"field": "embedding_model", "size": 10}},
    }


def _retrieval_diagnostic_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ranked_lane_diagnostic(
    hits: list[dict[str, Any]], *, score_field: str = "_score"
) -> dict[str, Any]:
    """Fingerprint one ranked lane without exposing caller-visible identities."""
    identities = [hit_identity(hit) for hit in hits]
    ranked_scores = [
        [identity, hit.get(score_field)] for identity, hit in zip(identities, hits, strict=True)
    ]
    return {
        "candidates": len(hits),
        "ordered_identities_sha256": _retrieval_diagnostic_sha256(identities),
        "membership_sha256": _retrieval_diagnostic_sha256(sorted(set(identities))),
        "ordered_scores_sha256": _retrieval_diagnostic_sha256(ranked_scores),
    }


def _is_exact_token_query(query: str) -> bool:
    """Return True for code/token-like queries that should not allow partial fuzzy matches."""
    if not query or len(query.strip()) < 3:
        return False

    query = query.strip()

    if re.search(r"[^a-zA-Z0-9\s]", query):
        return True

    return bool(re.search(r"[a-zA-Z]", query) and re.search(r"\d", query))


def _normalize_file_facet_aggregations(aggregations: dict[str, Any]) -> dict[str, Any]:
    """Normalize file facets without requiring the broader post-tag facet refactor."""
    normalized = dict(aggregations)
    for facet_name in (
        "data_sources",
        "document_types",
        "owners",
        "connector_types",
        "embedding_models",
    ):
        facet = aggregations.get(facet_name)
        if not isinstance(facet, dict):
            normalized[facet_name] = {"buckets": []}
            continue

        raw_buckets = facet.get("buckets", [])
        buckets = raw_buckets if isinstance(raw_buckets, list) else []
        normalized[facet_name] = {
            **facet,
            "buckets": [bucket for bucket in buckets if isinstance(bucket, dict)],
        }
    return normalized


def _apply_exact_match_file_filter(
    query: str,
    chunks: list[dict[str, Any]],
    aggregations: dict[str, Any],
    *,
    is_wildcard_match_all: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Narrow token-like exact matches while preserving ordinary RRF results."""
    normalized_query = query.strip().lower()
    if (
        not normalized_query
        or is_wildcard_match_all
        or len(normalized_query) < 4
        or not _is_exact_token_query(normalized_query)
    ):
        return chunks, aggregations

    exact_files = {
        filename
        for chunk in chunks
        for filename in [chunk.get("filename")]
        if isinstance(filename, str)
        and (
            normalized_query in filename.lower()
            or (
                isinstance(chunk.get("text"), str)
                and normalized_query in chunk.get("text", "").lower()
            )
        )
    }
    # A token-like query with no verbatim hit must retain the ranked results;
    # fuzzy retrieval may still contain the correct evidence.
    if not exact_files:
        return chunks, aggregations

    chunks = [chunk for chunk in chunks if chunk.get("filename") in exact_files]

    def _build_terms_agg(field: str, label_field: str | None = None) -> dict[str, Any]:
        files_by_value: dict[str, set[str]] = {}
        labels_by_value: dict[str, str] = {}
        for chunk in chunks:
            value = chunk.get(field)
            filename = chunk.get("filename")
            if not isinstance(value, str) or not value:
                continue
            if not isinstance(filename, str) or not filename:
                continue
            files_by_value.setdefault(value, set()).add(filename)
            if label_field:
                label = chunk.get(label_field)
                if isinstance(label, str) and label:
                    labels_by_value.setdefault(value, label)

        return {
            "doc_count_error_upper_bound": 0,
            "sum_other_doc_count": 0,
            "buckets": [
                {
                    "key": key,
                    "doc_count": len(filenames),
                    **({"label": labels_by_value.get(key, key)} if label_field else {}),
                }
                for key, filenames in sorted(files_by_value.items())
            ],
        }

    return chunks, {
        **aggregations,
        "data_sources": _build_terms_agg("filename"),
        "document_types": _build_terms_agg("mimetype"),
        "owners": _build_terms_agg("owner", label_field="owner_name"),
        "connector_types": _build_terms_agg("connector_type"),
        "embedding_models": _build_terms_agg("embedding_model"),
    }


def register_search_service(service: "SearchService") -> None:
    """
    Explicitly register the active search service for the @tool wrapper.
    This prevents stale instance risks and test interference.
    """
    global _global_search_service
    _global_search_service = service


_DLS_OPAQUE_RELATION_FIELDS = frozenset(
    {
        # Native/archive/filesystem metadata stays internal in v1.  This also
        # prevents paths, archive ids, parent ids, authors, or EXIF values from
        # leaking if a future query accidentally requests the whole _source.
        "document_metadata_profile",
        "document_metadata_profile_id",
        "document_metadata_profile_version",
        "document_metadata_facts_sha256",
        "document_metadata_extractor",
        "document_metadata_extractor_version",
        "document_metadata_backfill_status",
        "document_metadata_updated_at",
        "source_attachment",
        "source_relation_target_ids",
        "source_relation_roles",
        "scope_context_relations",
    }
)


def redact_dls_opaque_relation_metadata(value: Any, *, _in_provenance: bool = False) -> Any:
    """Remove relation metadata that can name a DLS-hidden target.

    Provenance relations are useful while the backend closes a caller-scoped
    graph, but the relation assertion belongs to the visible source document
    and can still name a target that the caller cannot read. Public retrieval,
    citation, and tool payloads therefore retain source-entity identity while
    omitting unresolved relation targets. Graph ``edges`` remain available:
    they are created only after both endpoint entities resolve through the
    same DLS-scoped OpenSearch client.
    """
    if isinstance(value, list):
        return [
            redact_dls_opaque_relation_metadata(item, _in_provenance=_in_provenance)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    redacted: dict[str, Any] = {}
    for field, field_value in value.items():
        if field in _DLS_OPAQUE_RELATION_FIELDS:
            continue
        if _in_provenance and field == "relations":
            continue
        if field == "context_edges":
            # Context-only targets are deliberately not graph-resolved, so
            # their visibility cannot be certified for the current caller.
            redacted[field] = []
            continue
        redacted[field] = redact_dls_opaque_relation_metadata(
            field_value,
            _in_provenance=field == "source_provenance",
        )
    return redacted


@tool
async def search_tool(query: str, embedding_model: str = None) -> dict[str, Any]:
    """
    Use this tool to search for documents relevant to the query.

    Args:
        query (str): query string to search the corpus
        embedding_model (str): Optional override for embedding model.
                              If not provided, uses the current embedding
                              model from configuration.

    Returns:
        dict (str, Any): {"results": [chunks]} on success
    """
    if not _global_search_service:
        logger.error("SearchService tool called before initialization")
        return {"results": [], "error": "Search service not available"}
    result = await _global_search_service.search_tool(query, embedding_model=embedding_model)
    return redact_dls_opaque_relation_metadata(result)


class SearchService:
    def __init__(self, session_manager=None, models_service=None):
        self.session_manager = session_manager
        self.models_service = models_service
        self._provenance_hidden_targets = self._resolve_hidden_provenance_targets
        self._configure_provider_env()

    async def _resolve_hidden_provenance_targets(self, reader, targets: list[str]) -> set[str]:
        from services.provenance_visibility import resolve_dls_hidden_targets

        control = clients.create_index_admin_opensearch_client()
        if control is None:
            return set()
        try:
            return await resolve_dls_hidden_targets(
                reader, control, index=get_index_name(), targets=targets
            )
        finally:
            await control.close()

    def _configure_provider_env(self):
        """Set provider env vars once at init time."""
        try:
            config = get_openrag_config()
            if config.providers.ollama.endpoint:
                fixed = transform_localhost_url(config.providers.ollama.endpoint)
                # Use setdefault to avoid clobbering existing env vars if they were
                # set explicitly via shell, but ensures we have a working default.
                os.environ.setdefault("OLLAMA_API_BASE", fixed)
                os.environ.setdefault("OLLAMA_BASE_URL", fixed)
        except Exception as e:
            logger.warning("[SEARCH] Could not configure Ollama endpoint from config", error=str(e))

    async def resolve_cited_chunks(
        self,
        chunk_ids: list[str],
        *,
        user_id: str | None,
        jwt_token: str | None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Hydrate exact non-streaming citations through the caller's DLS client.

        Langflow's non-streaming Responses payload may contain the assistant
        message but omit tool-call artifacts. The message still cites immutable
        chunk ids. Resolve only those ids against the authenticated OpenSearch
        view and preserve citation order. Missing or inaccessible ids disappear
        rather than becoming unverified source cards.
        """
        ordered_ids = list(dict.fromkeys(item for item in chunk_ids if isinstance(item, str)))
        if not ordered_ids:
            return []
        ordered_ids = ordered_ids[:100]

        filter_clauses: list[dict[str, Any]] = []
        field_mapping = {
            "data_sources": "filename",
            "document_types": "mimetype",
            "owners": "owner",
            "connector_types": "connector_type",
        }
        for filter_key, values in (filters or {}).items():
            if not isinstance(values, list):
                continue
            field_name = field_mapping.get(filter_key, filter_key)
            if not values:
                filter_clauses.append({"term": {field_name: "__IMPOSSIBLE_VALUE__"}})
            elif len(values) == 1:
                filter_clauses.append({"term": {field_name: values[0]}})
            else:
                filter_clauses.append({"terms": {field_name: values}})

        identity_query = {
            "bool": {
                "should": [
                    {"terms": {"chunk_id": ordered_ids}},
                    {"ids": {"values": ordered_ids}},
                ],
                "minimum_should_match": 1,
            }
        }
        query: dict[str, Any] = identity_query
        if filter_clauses:
            query = {"bool": {"must": [identity_query], "filter": filter_clauses}}

        source_fields = [
            "filename",
            "text",
            "mimetype",
            "page",
            "chunk_id",
            "document_id",
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
            "connector_file_id",
            "chunk_index",
            "chunking_strategy",
            "embedding_model",
            "embedding_dimensions",
            "parser",
            "chunk_size",
            "chunk_overlap",
        ]
        client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)
        response = await client.search(
            index=get_index_name(),
            body={
                "query": query,
                "_source": source_fields,
                "size": len(ordered_ids),
                "track_total_hits": False,
            },
        )

        by_cited_identity: dict[str, dict[str, Any]] = {}
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            stored_chunk_id = source.get("chunk_id") or hit.get("_id")
            if not isinstance(stored_chunk_id, str):
                continue
            hydrated = {
                **source,
                "chunk_id": stored_chunk_id,
                "id": stored_chunk_id,
                "score": 0,
            }
            by_cited_identity[stored_chunk_id] = hydrated
            hit_id = hit.get("_id")
            if isinstance(hit_id, str):
                by_cited_identity[hit_id] = hydrated

        resolved = [by_cited_identity[item] for item in ordered_ids if item in by_cited_identity]
        return redact_dls_opaque_relation_metadata(resolved)

    async def search_tool(
        self,
        query: str,
        embedding_model: str = None,
        *,
        group_by_document: bool = False,
        page: int = 1,
        page_size: int = 100,
        _discovery_query: DiscoveryQuery | None = None,
        _include_timing: bool = False,
        _metadata_restriction: MetadataCandidateRestriction | None = None,
    ) -> dict[str, Any]:
        """
        Use this tool to search for documents relevant to the query.

        Args:
            query (str): query string to search the corpus
            embedding_model (str): Optional override for embedding model.
                                  If not provided, uses the current embedding
                                  model from configuration.

        Returns:
            dict (str, Any): {"results": [chunks]} on success
        """
        from utils.embedding_fields import get_embedding_field_name

        search_started = time.perf_counter()
        embedding_seconds = 0.0
        lane_timings: dict[str, float] = {}
        fusion_seconds = 0.0

        document_page = max(1, int(page))
        document_page_size = min(1000, max(1, int(page_size)))
        document_offset = (document_page - 1) * document_page_size
        # OpenSearch's result window is 10,000 by default. The Knowledge UI
        # exposes at most 1,000 documents per page and receives an explicit
        # empty page rather than silently wrapping if the window is exceeded.
        document_window = document_offset + document_page_size
        if group_by_document and document_window > DOCUMENT_SEARCH_RESULT_WINDOW:
            raise ValueError("Document search pagination cannot exceed 10,000 results")

        # Strategy: Use provided model, or default to the configured embedding
        # model. This assumes documents are embedded with that model by default.
        # Future enhancement: Could auto-detect available models in corpus.
        openrag_config = get_openrag_config()
        retrieval_settings = RetrievalSettings.from_knowledge(openrag_config.knowledge)
        use_retrieval_v2 = retrieval_settings.strategy == "rrf"
        requested_profile = requested_retrieval_profile(retrieval_settings)
        effective_profile = _initial_effective_retrieval_profile(requested_profile)
        execution_warnings: list[dict[str, Any]] = []
        embedding_model = (
            embedding_model
            or get_embedding_model()
            or get_declared_default_embedding_model(openrag_config.knowledge.embedding_provider)
        )
        embedding_field_name = get_embedding_field_name(embedding_model)

        logger.info(
            "[SEARCH] Query started",
            embedding_model=embedding_model,
            embedding_field=embedding_field_name,
            query_preview=query[:50] if query else None,
            retrieval_strategy=retrieval_settings.strategy,
            retrieval_mode=retrieval_settings.mode,
        )

        # Get authentication context from the current async context
        user_id, jwt_token = get_auth_context()
        # Get search filters, limit, and score threshold from context
        from auth_context import (
            get_score_threshold,
            get_search_filters,
            get_search_limit,
        )

        filters = get_search_filters() or {}
        limit = get_search_limit()
        score_threshold = get_score_threshold()
        # Detect wildcard request ("*") to return global facets/stats without semantic search
        is_wildcard_match_all = isinstance(query, str) and query.strip() == "*"

        # Get available embedding models from corpus
        query_embeddings = {}
        available_models = []
        failed_models: list = []
        embedding_detection_error: str | None = None

        opensearch_client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)

        if use_retrieval_v2:
            # RRF is deterministic only when OpenSearch can apply its persisted
            # secondary ``chunk_id`` sort.  Validate the real mapping before
            # computing embeddings or sending either ranking lane.  Legacy
            # documents without a value remain valid via ``missing: _last``;
            # an incompatible *mapping* is a hard operational error.
            mapping_client = getattr(clients, "opensearch", None) or opensearch_client
            try:
                await require_sortable_chunk_id_mapping(mapping_client, get_index_name())
            except RRFMappingError as exc:
                logger.error("RRF blocked by incompatible chunk_id mapping", error=str(exc))
                return _finalize_retrieval_contract(
                    {"results": [], "error": str(exc), "retrieval_strategy": "rrf"},
                    requested=requested_profile,
                    effective=effective_profile,
                )

        if not is_wildcard_match_all:
            # Build filter clauses first so we can use them in model detection
            filter_clauses: list[dict[str, Any]] = []
            if filters:
                # Map frontend filter names to backend field names
                field_mapping = {
                    "data_sources": "filename",
                    "document_types": "mimetype",
                    "owners": "owner",
                    "connector_types": "connector_type",
                }

                for filter_key, values in filters.items():
                    if values is not None and isinstance(values, list):
                        # Map frontend key to backend field name
                        field_name = field_mapping.get(filter_key, filter_key)

                        if len(values) == 0:
                            # Empty array means "match nothing" - use impossible filter
                            filter_clauses.append({"term": {field_name: "__IMPOSSIBLE_VALUE__"}})
                        elif len(values) == 1:
                            # Single value filter
                            filter_clauses.append({"term": {field_name: values[0]}})
                        else:
                            # Multiple values filter
                            filter_clauses.append({"terms": {field_name: values}})

            try:
                # Build aggregation query with filters applied
                agg_query = {
                    "size": 0,
                    "aggs": {
                        "embedding_models": {"terms": {"field": "embedding_model", "size": 10}}
                    },
                }

                # Apply filters to model detection if any exist
                if filter_clauses:
                    agg_query["query"] = {"bool": {"filter": filter_clauses}}

                agg_result = await opensearch_client.search(
                    index=get_index_name(), body=agg_query, params={"terminate_after": 0}
                )
                buckets = (
                    agg_result.get("aggregations", {})
                    .get("embedding_models", {})
                    .get("buckets", [])
                )
                available_models = [b["key"] for b in buckets if b["key"]]

                if not available_models:
                    # Fallback to configured model if no documents indexed yet
                    available_models = [embedding_model]

                logger.info(
                    "Detected embedding models in corpus",
                    available_models=available_models,
                    model_counts={b["key"]: b["doc_count"] for b in buckets},
                    with_filters=len(filter_clauses) > 0,
                )
            except Exception as e:
                logger.warning(
                    "Failed to detect embedding models, using configured model", error=str(e)
                )
                embedding_detection_error = str(e)
                available_models = [embedding_model]

            # Parallelize embedding generation for all models
            async def embed_with_model(model_name):
                delay = EMBED_RETRY_INITIAL_DELAY
                attempts = 0
                last_exception = None

                # Use centralized utility for LiteLLM model formatting.
                # strict=True: if no configured provider claims this model
                # (e.g. the provider was removed after ingest), raise
                # immediately rather than entering a ~3s retry loop on an
                # unroutable model name.
                if self.models_service:
                    formatted_model = await self.models_service.get_litellm_model_name(
                        model_name, strict=True
                    )
                else:
                    # Fallback if service not injected (tests/etc)
                    formatted_model = model_name

                while attempts < MAX_EMBED_RETRIES:
                    attempts += 1
                    try:
                        resp = await clients.patched_embedding_client.embeddings.create(
                            model=formatted_model, input=[query]
                        )
                        # Try to get embedding - some providers return .embedding, others return ['embedding']
                        embedding = getattr(resp.data[0], "embedding", None)
                        if embedding is None:
                            embedding = resp.data[0]["embedding"]
                        return model_name, embedding
                    except Exception as e:
                        last_exception = e
                        if attempts >= MAX_EMBED_RETRIES:
                            logger.error(
                                "Failed to embed with model after retries",
                                model=model_name,
                                attempts=attempts,
                                error=str(e),
                            )
                            raise RuntimeError(f"Failed to embed with model {model_name}") from e

                        logger.warning(
                            "Retrying embedding generation",
                            model=model_name,
                            attempt=attempts,
                            max_attempts=MAX_EMBED_RETRIES,
                            error=str(e),
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, EMBED_RETRY_MAX_DELAY)

                # Should not reach here, but guard in case
                raise RuntimeError(f"Failed to embed with model {model_name}") from last_exception

            # Run all embeddings in parallel, tolerating per-model failures so
            # one broken model (e.g. provider credentials removed after ingest)
            # doesn't take down the entire search. If all models fail we fall
            # back to keyword-only search below.
            embedding_started = time.perf_counter()
            embedding_results = await asyncio.gather(
                *[embed_with_model(model) for model in available_models],
                return_exceptions=True,
            )
            embedding_seconds = time.perf_counter() - embedding_started

            for model_name, result in zip(available_models, embedding_results, strict=False):
                if isinstance(result, BaseException):
                    failed_models.append(model_name)
                    logger.warning(
                        "Skipping model with failed embedding; continuing with others",
                        model=model_name,
                        error=str(result),
                    )
                    continue
                if isinstance(result, tuple) and result[1] is not None:
                    successful_model, embedding = result
                    query_embeddings[successful_model] = embedding

            logger.info(
                "Generated query embeddings",
                models=list(query_embeddings.keys()),
                failed_models=failed_models,
                query_preview=query[:50],
            )
            dense_execution = effective_profile["lanes"]["dense"]
            dense_execution.update(
                {
                    "embedding_models": sorted(str(model) for model in query_embeddings),
                    "embedding_dimensions": {
                        str(model): len(vector)
                        for model, vector in query_embeddings.items()
                        if isinstance(vector, (list, tuple))
                    },
                    "failed_embedding_models": sorted(str(model) for model in failed_models),
                }
            )
            if requested_profile["lanes"]["dense"] == "required" and (
                not query_embeddings or failed_models or embedding_detection_error
            ):
                dense_execution["status"] = "failed"
                dense_execution["error"] = (
                    "embedding_model_detection_failed"
                    if embedding_detection_error
                    else "embedding_generation_failed"
                )
                execution_warnings.append(
                    {
                        "code": "retrieval_dense_lane_failed",
                        "failed_models": sorted(str(model) for model in failed_models),
                        "message": (
                            "Dense retrieval is incomplete; partial results may be available, "
                            "but the requested retrieval profile is not certifiable."
                        ),
                    }
                )
        else:
            # Wildcard query - no embedding needed
            filter_clauses = []
            if filters:
                # Map frontend filter names to backend field names
                field_mapping = {
                    "data_sources": "filename",
                    "document_types": "mimetype",
                    "owners": "owner",
                    "connector_types": "connector_type",
                }

                for filter_key, values in filters.items():
                    if values is not None and isinstance(values, list):
                        # Map frontend key to backend field name
                        field_name = field_mapping.get(filter_key, filter_key)

                        if len(values) == 0:
                            # Empty array means "match nothing" - use impossible filter
                            filter_clauses.append({"term": {field_name: "__IMPOSSIBLE_VALUE__"}})
                        elif len(values) == 1:
                            # Single value filter
                            filter_clauses.append({"term": {field_name: values[0]}})
                        else:
                            # Multiple values filter
                            filter_clauses.append({"terms": {field_name: values}})

        # Build query body
        if is_wildcard_match_all:
            # Match all documents; still allow filters to narrow scope
            if filter_clauses:
                query_block: dict[str, Any] = {"bool": {"filter": filter_clauses}}
            else:
                query_block = {"match_all": {}}
        else:
            # Build multi-model KNN queries (only for models that successfully
            # produced query embeddings)
            knn_queries = []
            embedding_fields_to_check = []

            for model_name, embedding_vector in query_embeddings.items():
                field_name = get_embedding_field_name(model_name)
                embedding_fields_to_check.append(field_name)
                # Document browsing needs a fixed window so pagination does
                # not change membership. Ranked Retrieval v2 instead honors
                # its configured dense candidate horizon; otherwise values
                # above the historical default of 50 are silently ineffective.
                knn_result_count = (
                    DOCUMENT_SEARCH_RESULT_WINDOW
                    if group_by_document
                    else retrieval_settings.vector_candidates
                )
                knn_queries.append(
                    {
                        "knn": {
                            field_name: {
                                "vector": embedding_vector,
                                "k": knn_result_count,
                                "num_candidates": max(1000, knn_result_count),
                            }
                        }
                    }
                )

            # Only require an embedding field when we actually have embeddings
            # to match against — otherwise we'd filter out every doc in keyword
            # fallback mode.
            all_filters = list(filter_clauses)
            if knn_queries:
                exists_should: list[dict[str, Any]] = [
                    {"exists": {"field": f}} for f in embedding_fields_to_check
                ]
                # Docs indexed under a failed provider have none of the successful
                # embedding fields, but keyword matching should still surface them.
                # Allow them through by matching on their embedding_model value.
                if failed_models:
                    exists_should.append({"terms": {"embedding_model": failed_models}})
                all_filters.append(
                    {
                        "bool": {
                            "should": exists_should,
                            "minimum_should_match": 1,
                        }
                    }
                )

            logger.debug(
                "Building hybrid query with filters",
                user_filters_count=len(filter_clauses),
                total_filters_count=len(all_filters),
                filter_types=[type(f).__name__ for f in all_filters],
                knn_queries_count=len(knn_queries),
            )

            # Hybrid search (semantic + keyword) when embeddings are available;
            # keyword-only fallback when none succeeded. When falling back, bump
            # the multi_match boost so keyword scoring isn't artificially damped.
            should_clauses = []
            if knn_queries:
                should_clauses.append(
                    {
                        "dis_max": {
                            "tie_breaker": 0.0,  # Take only the best match, no blending
                            "boost": 0.7,  # 70% weight for semantic search
                            "queries": knn_queries,
                        }
                    }
                )
            should_clauses.extend(
                [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["text^2", "filename^1.5"],
                            "type": "best_fields",
                            "operator": "or",
                            "fuzziness": "AUTO:4,7",
                            "boost": 0.3 if knn_queries else 1.0,
                        }
                    },
                    {
                        # Prefix fallback for partial input (e.g. "vita" -> "vitamin").
                        # Avoid bool_prefix here because our current mappings are:
                        # - text: standard "text" (not search_as_you_type / edge-ngram)
                        # - filename: "keyword"
                        # match_phrase_prefix with a bounded expansion is safer.
                        "match_phrase_prefix": {
                            "text": {
                                "query": query,
                                "max_expansions": 50,
                                "boost": 0.25,
                            }
                        }
                    },
                ]
            )

            query_block = {
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1,
                    "filter": all_filters,
                }
            }

        source_fields = [
            "chunk_id",
            "document_id",
            "filename",
            "mimetype",
            "page",
            "chunk_index",
            "chunking_strategy",
            "text",
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
            "connector_file_id",
            "owner",
            "owner_name",
            "owner_email",
            "file_size",
            "connector_type",
            "embedding_model",
            "embedding_dimensions",
            "parser",
            "chunk_size",
            "chunk_overlap",
            "document_profile_version",
            "document_chunk_count",
            "document_page_count",
            "document_max_page",
            "document_character_count",
            "document_size_class",
            "allowed_users",
            "allowed_groups",
            "allowed_principal_labels",
        ]
        search_body: dict[str, Any] = {
            "query": query_block,
            "aggs": _build_file_facet_aggregations(),
            "_source": source_fields,
            "size": limit,
            # OpenSearch does not guarantee the order of equal-score hits.
            # Keep a persistent chunk identity as the secondary sort so RRF
            # receives stable ranked lanes across equivalent executions.
            "sort": [
                {"_score": {"order": "desc"}},
                # ``_id`` cannot be sorted by OpenSearch.  New backend-indexed
                # chunks persist this keyword/doc_values field; legacy chunks
                # sort last and retain best-effort rank compatibility.
                {"chunk_id": {"order": "asc", "missing": "_last"}},
            ],
        }
        if group_by_document:
            # The Knowledge table is a document browser, not a chunk browser.
            # Collapse keeps the best matching chunk as the representative row.
            # A terms aggregation enumerates the same ranked document window so
            # the total is deterministic; cardinality aggregations are only
            # approximate and made the displayed total drift between pages.
            search_body.update(
                {
                    "from": document_offset,
                    "size": document_page_size,
                    "collapse": {"field": "filename"},
                }
            )
            search_body["aggs"]["document_names"] = {
                "terms": {
                    "field": "filename",
                    "size": DOCUMENT_SEARCH_RESULT_WINDOW,
                    "shard_size": DOCUMENT_SEARCH_RESULT_WINDOW,
                }
            }

        # Add score threshold only for hybrid (not meaningful for match_all)
        if not is_wildcard_match_all and score_threshold > 0:
            search_body["min_score"] = score_threshold

        # In RRF mode lexical and vector candidates are intentionally fetched
        # by separate OpenSearch requests.  Their score scales are unrelated;
        # only their ranks are fused below.  Weighted remains available as an
        # explicit compatibility strategy, while RRF is the Standard default.
        retrieval_bodies: list[tuple[str, dict[str, Any]]] = []
        if use_retrieval_v2 and not is_wildcard_match_all and not group_by_document:
            lexical_body: dict[str, Any] = {
                "query": {
                    "bool": {
                        "should": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": ["text^2", "filename^1.5"],
                                    "type": "best_fields",
                                    "operator": "or",
                                    "fuzziness": "AUTO:4,7",
                                }
                            },
                            {
                                "match_phrase_prefix": {
                                    "text": {"query": query, "max_expansions": 50}
                                }
                            },
                        ],
                        "minimum_should_match": 1,
                        "filter": filter_clauses,
                    }
                },
                "aggs": _build_file_facet_aggregations(),
                "_source": source_fields,
                "size": retrieval_settings.lexical_candidates,
                "sort": [
                    {"_score": {"order": "desc"}},
                    {"chunk_id": {"order": "asc", "missing": "_last"}},
                ],
            }
            vector_body: dict[str, Any] = {
                "query": {
                    "bool": {
                        "should": [{"dis_max": {"tie_breaker": 0.0, "queries": knn_queries}}],
                        "minimum_should_match": 1,
                        "filter": all_filters,
                    }
                },
                "aggs": _build_file_facet_aggregations(),
                "_source": source_fields,
                "size": retrieval_settings.vector_candidates,
                "sort": [
                    {"_score": {"order": "desc"}},
                    {"chunk_id": {"order": "asc", "missing": "_last"}},
                ],
            }
            if score_threshold > 0:
                lexical_body["min_score"] = score_threshold
                vector_body["min_score"] = score_threshold
            if retrieval_settings.mode in {"hybrid", "lexical"}:
                retrieval_bodies.append(("lexical", lexical_body))
            if retrieval_settings.mode in {"hybrid", "vector"} and knn_queries:
                retrieval_bodies.append(("vector", vector_body))
            # Vector-only cannot run if every embedding provider is unavailable.
            if not retrieval_bodies:
                retrieval_bodies.append(("lexical", lexical_body))

        def without_num_candidates(body: dict[str, Any]) -> dict[str, Any] | None:
            """Return a compatibility retry body for older OpenSearch nodes."""
            if not query_embeddings:
                return None
            fallback = copy.deepcopy(body)
            removed = False

            def walk(value: Any) -> None:
                nonlocal removed
                if isinstance(value, dict):
                    if "num_candidates" in value:
                        value.pop("num_candidates")
                        removed = True
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(fallback)
            return fallback if removed else None

        # Authentication required - ACL filter is applied at the application layer above
        logger.debug(
            "search_service authentication info",
            user_id=user_id,
            has_jwt_token=jwt_token is not None,
        )
        if not user_id:
            logger.warning("[SEARCH] user_id missing, rejecting search request")
            return {"results": [], "error": "Authentication required"}

        # Get user's OpenSearch client with JWT for OIDC auth through session manager
        opensearch_client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)

        from opensearchpy.exceptions import RequestError

        from utils.opensearch_utils import (
            DISK_SPACE_ERROR_MESSAGE,
            OpenSearchDiskSpaceError,
            is_disk_space_error,
        )

        search_params = {"terminate_after": 0}
        lane_request_diagnostics: dict[str, dict[str, Any]] = {}

        async def execute_one_search(
            body: dict[str, Any], label: str
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            fallback_body = without_num_candidates(body)
            initial_fingerprint = _retrieval_diagnostic_sha256(body)
            try:
                index_name = get_index_name()
                logger.info("Sending query to index", retrieval_lane=label, index_name=index_name)
                response = await opensearch_client.search(
                    index=index_name, body=body, params=search_params
                )
                response["_execution_failures"] = validate_search_response(response)
                return response, {
                    "initial_request_sha256": initial_fingerprint,
                    "executed_request_sha256": initial_fingerprint,
                    "compatibility_retry_without_num_candidates": False,
                }
            except RequestError as error:
                error_message = str(error)
                if is_disk_space_error(error):
                    logger.error(
                        "OpenSearch query blocked by disk space constraint", error=error_message
                    )
                    raise OpenSearchDiskSpaceError(DISK_SPACE_ERROR_MESSAGE) from error
                if (
                    fallback_body is not None
                    and "unknown field [num_candidates]" in error_message.lower()
                ):
                    logger.warning(
                        "OpenSearch cluster does not support num_candidates; retrying without it",
                        retrieval_lane=label,
                    )
                    try:
                        response = await opensearch_client.search(
                            index=get_index_name(), body=fallback_body, params=search_params
                        )
                        response["_execution_failures"] = validate_search_response(response)
                        return response, {
                            "initial_request_sha256": initial_fingerprint,
                            "executed_request_sha256": _retrieval_diagnostic_sha256(fallback_body),
                            "compatibility_retry_without_num_candidates": True,
                        }
                    except RequestError as retry_error:
                        if is_disk_space_error(retry_error):
                            logger.error(
                                "OpenSearch retry blocked by disk space constraint",
                                error=str(retry_error),
                            )
                            raise OpenSearchDiskSpaceError(
                                DISK_SPACE_ERROR_MESSAGE
                            ) from retry_error
                        logger.error(
                            "OpenSearch retry without num_candidates failed",
                            error=str(retry_error),
                            retrieval_lane=label,
                        )
                        raise
                logger.error(
                    "OpenSearch query failed",
                    error=error_message,
                    retrieval_lane=label,
                )
                raise
            except OpenSearchDiskSpaceError:
                raise
            except Exception as error:
                if is_disk_space_error(error):
                    logger.error(
                        "OpenSearch query blocked by disk space constraint", error=str(error)
                    )
                    raise OpenSearchDiskSpaceError(DISK_SPACE_ERROR_MESSAGE) from error
                logger.error("OpenSearch query failed", error=str(error), retrieval_lane=label)
                raise

        async def execute_search(body: dict[str, Any], label: str) -> dict[str, Any]:
            lane_started = time.perf_counter()
            if _metadata_restriction is None:
                response, diagnostic = await execute_one_search(body, label)
                lane_request_diagnostics[label] = diagnostic
                lane_timings[label] = time.perf_counter() - lane_started
                return response

            partition_diagnostics: list[dict[str, Any]] = []

            async def execute_partition(restricted_body: dict[str, Any]) -> dict[str, Any]:
                response, diagnostic = await execute_one_search(restricted_body, label)
                partition_diagnostics.append(diagnostic)
                return response

            response = await execute_metadata_restricted_lane(
                body,
                _metadata_restriction,
                execute=execute_partition,
            )
            lane_request_diagnostics[label] = {
                "initial_request_sha256": _retrieval_diagnostic_sha256(body),
                "metadata_restricted": True,
                "metadata_filter_sha256": (_metadata_restriction.diagnostics.filter_sha256),
                "eligible_occurrences": (_metadata_restriction.diagnostics.eligible_count),
                "partitions": len(partition_diagnostics),
                "partition_requests_sha256": _retrieval_diagnostic_sha256(partition_diagnostics),
                "compatibility_retry_without_num_candidates": any(
                    item["compatibility_retry_without_num_candidates"]
                    for item in partition_diagnostics
                ),
            }
            lane_timings[label] = time.perf_counter() - lane_started
            return response

        retrieval_results: dict[str, dict[str, Any]] = {}
        if retrieval_bodies:
            lanes = [name for name, _body in retrieval_bodies]
            lane_results = await asyncio.gather(
                *[execute_search(body, name) for name, body in retrieval_bodies],
                return_exceptions=True,
            )
            for lane, lane_result in zip(lanes, lane_results, strict=True):
                contract_lane = "dense" if lane == "vector" else lane
                lane_execution = effective_profile["lanes"][contract_lane]
                if isinstance(lane_result, OpenSearchDiskSpaceError):
                    raise lane_result
                if isinstance(lane_result, BaseException):
                    lane_execution.update({"status": "failed", "error": str(lane_result)})
                    execution_warnings.append(
                        {
                            "code": _RETRIEVAL_LANE_FAILURE_CODES[contract_lane],
                            "lane": contract_lane,
                            "message": (
                                f"The {contract_lane} retrieval lane failed; partial results "
                                "may be available, but scope certification is disabled."
                            ),
                        }
                    )
                    continue
                retrieval_results[lane] = lane_result
                candidate_count = len(lane_result.get("hits", {}).get("hits", []))
                lane_execution["candidates"] = candidate_count
                response_failures = lane_result.get("_execution_failures", [])
                if response_failures:
                    lane_execution.update(
                        {"status": "failed", "error": ", ".join(response_failures)}
                    )
                    execution_warnings.append(
                        {
                            "code": _RETRIEVAL_LANE_FAILURE_CODES[contract_lane],
                            "lane": contract_lane,
                            "message": ", ".join(response_failures),
                        }
                    )
                    continue
                # A successful vector request cannot repair missing/failed
                # embeddings for another model in the requested dense lane.
                if contract_lane != "dense" or (
                    query_embeddings and not failed_models and not embedding_detection_error
                ):
                    lane_execution.update({"status": "succeeded"})
                    lane_execution.pop("error", None)
            if not retrieval_results:
                return _finalize_retrieval_contract(
                    {
                        "results": [],
                        "error": "All requested retrieval lanes failed",
                        "retrieval_strategy": retrieval_settings.strategy,
                    },
                    requested=requested_profile,
                    effective=effective_profile,
                    warnings=execution_warnings,
                )
            results = retrieval_results.get("lexical") or next(iter(retrieval_results.values()))
        else:
            results = await execute_search(search_body, "weighted")
            weighted_candidates = len(results.get("hits", {}).get("hits", []))
            effective_profile["lanes"]["lexical"].update(
                {"status": "succeeded", "candidates": weighted_candidates}
            )
            effective_profile["lanes"]["lexical"].pop("error", None)
            if query_embeddings:
                effective_profile["lanes"]["dense"]["candidates"] = weighted_candidates
                if not failed_models and not embedding_detection_error:
                    effective_profile["lanes"]["dense"]["status"] = "succeeded"
                    effective_profile["lanes"]["dense"].pop("error", None)

        if not retrieval_results and results.get("_execution_failures"):
            for lane in ("lexical", "dense"):
                if effective_profile["lanes"][lane]["requested"]:
                    effective_profile["lanes"][lane].update(
                        {"status": "failed", "error": ", ".join(results["_execution_failures"])}
                    )

        raw_hits = results.get("hits", {}).get("hits", [])
        retrieval_diagnostics: dict[str, Any] | None = None
        if retrieval_results:
            fusion_started = time.perf_counter()
            ranked_lists = [
                lane_result.get("hits", {}).get("hits", [])
                for lane_result in retrieval_results.values()
            ]
            try:
                raw_hits = reciprocal_rank_fusion(ranked_lists, k=retrieval_settings.rrf_k)
                if requested_profile["lanes"]["fusion"] == "required":
                    required_input_lanes_succeeded = all(
                        effective_profile["lanes"][lane]["status"] == "succeeded"
                        for lane in ("lexical", "dense")
                    )
                    fusion_execution = effective_profile["lanes"]["fusion"]
                    if required_input_lanes_succeeded:
                        fusion_execution.update(
                            {"status": "succeeded", "candidates": len(raw_hits)}
                        )
                        fusion_execution.pop("error", None)
                    else:
                        fusion_execution.update(
                            {"status": "failed", "error": "required_lane_failed"}
                        )
            except Exception as exc:
                # Preserve one successful lane for diagnosis, but never present
                # it as execution of the requested fused profile.
                raw_hits = list(ranked_lists[0]) if ranked_lists else []
                fusion_execution = effective_profile["lanes"]["fusion"]
                fusion_execution.update({"status": "failed", "error": str(exc)})
                execution_warnings.append(
                    {
                        "code": "retrieval_fusion_failed",
                        "message": (
                            "Retrieval fusion failed; unfused partial results are available, "
                            "but scope certification is disabled."
                        ),
                    }
                )
            if _discovery_query is not None:
                lane_rank_by_identity = {
                    lane: {
                        hit_identity(hit): rank
                        for rank, hit in enumerate(
                            lane_result.get("hits", {}).get("hits", []), start=1
                        )
                    }
                    for lane, lane_result in retrieval_results.items()
                }
                for rrf_rank, hit in enumerate(raw_hits, start=1):
                    identity = hit_identity(hit)
                    lexical_rank = lane_rank_by_identity.get("lexical", {}).get(identity)
                    vector_rank = lane_rank_by_identity.get("vector", {}).get(identity)
                    matched_lanes = [
                        lane
                        for lane, rank in (("lexical", lexical_rank), ("dense", vector_rank))
                        if rank is not None
                    ]
                    hit["_retrieval_query_contribution"] = {
                        **_discovery_query.as_dict(),
                        "lexical_rank": lexical_rank,
                        "dense_rank": vector_rank,
                        "rrf_rank": rrf_rank,
                        "matched_lanes": matched_lanes,
                        "query_rrf_score": hit.get("_retrieval_fusion_score"),
                    }
            public_lane_diagnostics = {
                ("dense" if lane == "vector" else lane): {
                    **_ranked_lane_diagnostic(lane_result.get("hits", {}).get("hits", [])),
                    "request": lane_request_diagnostics.get(lane),
                }
                for lane, lane_result in retrieval_results.items()
            }
            fusion_diagnostic = _ranked_lane_diagnostic(
                raw_hits, score_field="_retrieval_fusion_score"
            )
            retrieval_diagnostics = {
                "contract_id": "openrag.retrieval-lane-diagnostics",
                "contract_version": 1,
                "guarantee": "deterministic_fusion_for_identical_ordered_input_lanes",
                "query_vectors_sha256": _retrieval_diagnostic_sha256(query_embeddings),
                "lanes": public_lane_diagnostics,
                "fusion": {
                    **fusion_diagnostic,
                    "ordered_input_lanes_sha256": _retrieval_diagnostic_sha256(
                        {
                            lane: diagnostic["ordered_scores_sha256"]
                            for lane, diagnostic in sorted(public_lane_diagnostics.items())
                        }
                    ),
                },
            }
            raw_hits = limit_chunks_per_document(
                raw_hits,
                max_chunks_per_document=retrieval_settings.max_chunks_per_document,
                adaptive_max_chunks_per_document=(
                    retrieval_settings.adaptive_max_chunks_per_document
                ),
            )
            raw_hits = await HttpReranker(
                retrieval_settings.reranker_url,
                retrieval_settings.reranker_timeout,
            ).rerank(query, raw_hits)
            raw_hits = raw_hits[:limit]
            fusion_seconds = time.perf_counter() - fusion_started

        # Transform results (keep for backward compatibility)
        chunks = []
        for hit in raw_hits:
            source = hit.get("_source", {})
            chunk = {
                "document_id": source.get("document_id"),
                "filename": source.get("filename"),
                "mimetype": source.get("mimetype"),
                "page": source.get("page"),
                "chunk_index": source.get("chunk_index"),
                "chunking_strategy": source.get("chunking_strategy"),
                "text": source.get("text"),
                "score": (
                    hit.get("_retrieval_rerank_score")
                    or hit.get("_retrieval_fusion_score")
                    or hit.get("_score")
                ),
                "source_url": source.get("source_url"),
                "connector_file_id": source.get("connector_file_id"),
                "source_provenance": source.get("source_provenance"),
                "source_entity_id": source.get("source_entity_id"),
                "source_entity_type": source.get("source_entity_type"),
                "source_entity_system": source.get("source_entity_system"),
                "source_entity_alternate_ids": source.get("source_entity_alternate_ids", []),
                "source_relation_target_ids": source.get("source_relation_target_ids", []),
                "source_relation_roles": source.get("source_relation_roles", []),
                "source_relative_path": source.get("source_relative_path"),
                "source_path_ancestors": source.get("source_path_ancestors", []),
                "owner": source.get("owner"),
                "owner_name": source.get("owner_name"),
                "owner_email": source.get("owner_email"),
                "file_size": source.get("file_size"),
                "connector_type": source.get("connector_type"),
                "embedding_model": source.get("embedding_model"),  # Include in results
                "embedding_dimensions": source.get("embedding_dimensions"),
                "parser": source.get("parser"),
                "chunk_size": source.get("chunk_size"),
                "chunk_overlap": source.get("chunk_overlap"),
                "document_profile_version": source.get("document_profile_version"),
                "document_chunk_count": source.get("document_chunk_count"),
                "document_page_count": source.get("document_page_count"),
                "document_max_page": source.get("document_max_page"),
                "document_character_count": source.get("document_character_count"),
                "document_size_class": source.get("document_size_class"),
                # Legacy chunks predate the source ``chunk_id`` mapping;
                # keep their existing primary id visible to callers.
                "chunk_id": source.get("chunk_id") or hit.get("_id"),
                "id": hit.get("_id"),
                # ACL fields (may be missing for some documents)
                "allowed_users": source.get("allowed_users", []),
                "allowed_groups": source.get("allowed_groups", []),
                "allowed_principal_labels": source.get("allowed_principal_labels", []),
            }
            contribution = hit.get("_retrieval_query_contribution")
            if isinstance(contribution, dict):
                chunk.update(
                    {
                        "matched_queries": [contribution["query_id"]],
                        "matched_lanes": list(contribution.get("matched_lanes", [])),
                        "best_rank_per_query": {
                            contribution["query_id"]: contribution.get("rrf_rank")
                        },
                        "query_contributions": [dict(contribution)],
                        "fusion_score": hit.get("_retrieval_fusion_score"),
                    }
                )
            chunks.append(chunk)

        # Preserve ordinary hybrid/RRF results. Exact narrowing is only for
        # identifier-like queries with an actual verbatim match.
        pre_filter_document_count = len(
            {chunk.get("filename") for chunk in chunks if isinstance(chunk.get("filename"), str)}
        )
        raw_aggregations = results.get("aggregations", {})
        document_names_aggregation = raw_aggregations.get("document_names", {})
        public_aggregations = {
            name: value for name, value in raw_aggregations.items() if name != "document_names"
        }
        chunks, aggregations = _apply_exact_match_file_filter(
            query,
            chunks,
            _normalize_file_facet_aggregations(public_aggregations),
            is_wildcard_match_all=is_wildcard_match_all,
        )

        # Return both transformed results and aggregations. Surface degraded
        # semantic-search signals so the UI can show a non-fatal warning
        # instead of treating partial-embedding failure as a hard error.
        response: dict[str, Any] = {
            "results": chunks,
            "aggregations": aggregations,
            "total": len(chunks),
        }
        if group_by_document:
            post_filter_document_count = len(
                {
                    chunk.get("filename")
                    for chunk in chunks
                    if isinstance(chunk.get("filename"), str)
                }
            )
            document_name_buckets = document_names_aggregation.get("buckets")
            document_total = (
                len(document_name_buckets)
                if isinstance(document_name_buckets, list)
                else post_filter_document_count
            )
            document_total_capped = bool(document_names_aggregation.get("sum_other_doc_count", 0))
            # Identifier-like searches can deliberately narrow the returned
            # page to verbatim matches after OpenSearch ranking. In that small
            # special case, do not expose the broader pre-filter document count.
            if post_filter_document_count < pre_filter_document_count:
                document_total = post_filter_document_count
            response.update(
                {
                    "total_documents": document_total,
                    "total_documents_capped": document_total_capped,
                    "page": document_page,
                    "page_size": document_page_size,
                }
            )
        if failed_models:
            response["warnings"] = [
                {
                    "code": "embedding_unavailable",
                    "models": failed_models,
                    "semantic_search_available": bool(query_embeddings),
                    "message": (
                        "Some documents were embedded with models that are "
                        "no longer reachable (provider removed or misconfigured). "
                        "Results shown use keyword matching only for those models."
                        if not query_embeddings
                        else "Semantic search is degraded for some embedding models."
                    ),
                }
            ]
        if retrieval_diagnostics is not None:
            response["retrieval_diagnostics"] = retrieval_diagnostics
        if retrieval_results and retrieval_settings.debug:
            response["retrieval_debug"] = {
                "strategy": "rrf",
                "mode": retrieval_settings.mode,
                "lanes": {
                    lane: len(lane_result.get("hits", {}).get("hits", []))
                    for lane, lane_result in retrieval_results.items()
                },
                "rrf_k": retrieval_settings.rrf_k,
                "max_chunks_per_document": retrieval_settings.max_chunks_per_document,
                "adaptive_max_chunks_per_document": (
                    retrieval_settings.adaptive_max_chunks_per_document
                ),
                "reranker_enabled": bool(retrieval_settings.reranker_url),
            }
        if _include_timing:
            response["_retrieval_timing"] = {
                "embedding_seconds": embedding_seconds,
                "lexical_seconds": lane_timings.get("lexical", 0.0),
                "dense_seconds": lane_timings.get("vector", 0.0),
                "fusion_seconds": fusion_seconds,
                "total_seconds": time.perf_counter() - search_started,
            }
        if _metadata_restriction is not None:
            response["metadata_filter"] = {
                **_metadata_restriction.diagnostics.model_dump(mode="json"),
                "projection_alias": _metadata_restriction.projection_alias,
            }
        return _finalize_retrieval_contract(
            response,
            requested=requested_profile,
            effective=effective_profile,
            warnings=execution_warnings,
        )

    async def _generate_discovery_plan(
        self,
        query: str,
        *,
        max_queries: int,
    ) -> tuple[list[DiscoveryQuery], str | None, float, dict[str, Any]]:
        """Generate variants from the user query alone and fail safely to q0."""

        started = time.perf_counter()
        bounded_max = min(MAX_DISCOVERY_QUERIES, max(1, int(max_queries)))
        if bounded_max == 1:
            plan = build_discovery_plan(query, None, max_queries=1)
            return (
                plan,
                None,
                0.0,
                {
                    **discovery_plan_audit(query, None, plan),
                    "planner_invoked": False,
                    "request_parameters": {},
                    "request_fingerprint": None,
                    "response_model": None,
                },
            )
        request_parameters: dict[str, Any] = {}
        request_fingerprint: str | None = None
        try:
            config = get_openrag_config()
            provider, model, _source = resolve_planner_selection(config)
            if not model:
                raise RuntimeError("No language model is configured for query decomposition")
            formatted_model = (
                await self.models_service.get_litellm_model_name(model, provider=provider or None)
                if self.models_service
                else model
            )
            request = build_responses_request(
                provider=provider or None,
                model=formatted_model,
                input=discovery_query_prompt(query, max_queries=bounded_max),
                stream=False,
                temperature=0,
                max_output_tokens=800,
            )
            request_parameters = {key: value for key, value in request.items() if key != "input"}
            request_parameters["input_sha256"] = hashlib.sha256(
                str(request["input"]).encode("utf-8")
            ).hexdigest()
            request_fingerprint = hashlib.sha256(
                json.dumps(
                    request_parameters,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            response = await clients.patched_llm_client.responses.create(**request)
            output_text = getattr(response, "output_text", None)
            if not isinstance(output_text, str) or not output_text.strip():
                raise ValueError("Query planner response has no output_text")
            plan = build_discovery_plan(
                query,
                output_text,
                max_queries=bounded_max,
                generation_method="llm_structured_v1",
            )
            return (
                plan,
                None,
                time.perf_counter() - started,
                {
                    **discovery_plan_audit(query, output_text, plan),
                    "planner_invoked": True,
                    "request_parameters": request_parameters,
                    "request_fingerprint": request_fingerprint,
                    "response_model": getattr(response, "model", None),
                },
            )
        except Exception as exc:
            logger.warning(
                "Multi-query generation failed; retaining original query", error=str(exc)
            )
            plan = build_discovery_plan(query, None, max_queries=bounded_max)
            return (
                plan,
                str(exc),
                time.perf_counter() - started,
                {
                    **discovery_plan_audit(query, None, plan),
                    "planner_invoked": bool(request_parameters),
                    "request_parameters": request_parameters,
                    "request_fingerprint": request_fingerprint,
                    "response_model": None,
                },
            )

    async def _search_multi_query(
        self,
        query: str,
        *,
        embedding_model: str | None,
        max_queries: int,
        concurrency: int,
        metadata_restriction: MetadataCandidateRestriction | None = None,
    ) -> dict[str, Any]:
        """Run one bounded discovery plan under the caller's existing DLS context."""

        started = time.perf_counter()
        config = get_openrag_config()
        settings = RetrievalSettings.from_knowledge(config.knowledge)
        planner_provider, planner_model, planner_source = resolve_planner_selection(config)
        planner_contract = {
            "provider": planner_provider.casefold(),
            "model": planner_model,
            "source": planner_source,
            "capability_profile": model_capability_profile(
                provider=planner_provider, model=planner_model
            ),
        }
        if settings.strategy != "rrf":
            raise ValueError("multi-query discovery requires the Retrieval v2 RRF strategy")
        requested_profile = requested_retrieval_profile(
            settings,
            multi_query_requested=True,
            multi_query_max_queries=max_queries,
        )
        effective_profile = _initial_effective_retrieval_profile(requested_profile)
        (
            plan,
            generation_error,
            generation_seconds,
            plan_audit,
        ) = await self._generate_discovery_plan(
            query,
            max_queries=max_queries,
        )
        planner_contract.update(
            {
                "request_parameters": plan_audit["request_parameters"],
                "request_fingerprint": plan_audit["request_fingerprint"],
                "response_model": plan_audit["response_model"],
            }
        )

        semaphore = asyncio.Semaphore(min(MAX_DISCOVERY_QUERIES, max(1, int(concurrency))))

        async def retrieve(item: DiscoveryQuery) -> tuple[DiscoveryQuery, dict[str, Any]]:
            async with semaphore:
                return item, await self.search_tool(
                    item.query_text,
                    embedding_model=embedding_model,
                    _discovery_query=item,
                    _include_timing=True,
                    _metadata_restriction=metadata_restriction,
                )

        raw_responses = await asyncio.gather(
            *[retrieve(item) for item in plan],
            return_exceptions=True,
        )
        successful: list[tuple[DiscoveryQuery, dict[str, Any]]] = []
        query_errors: list[dict[str, str]] = []
        for item, query_result in zip(plan, raw_responses, strict=True):
            if isinstance(query_result, BaseException):
                query_errors.append({"query_id": item.query_id, "error": str(query_result)})
                continue
            returned_query, response = query_result
            if response.get("error"):
                query_errors.append(
                    {"query_id": returned_query.query_id, "error": str(response["error"])}
                )
                continue
            successful.append((returned_query, response))

        original_response = next(
            (response for item, response in successful if item.query_id == "q0"),
            None,
        )
        if original_response is None:
            error = next(
                (value["error"] for value in query_errors if value["query_id"] == "q0"),
                "Original query retrieval failed",
            )
            effective_profile["lanes"]["multi_query"].update(
                {"status": "failed", "error": "original_query_failed"}
            )
            failure_code = (
                "multi_query_planner_failed" if generation_error else "multi_query_query_failed"
            )
            return _finalize_retrieval_contract(
                {
                    "results": [],
                    "error": error,
                    "retrieval_strategy": "rrf",
                    "discovery": {
                        "multi_query_requested": True,
                        "multi_query_executed": False,
                        "multi_query_query_count": 0,
                        "multi_query_status": "planner_failed"
                        if generation_error
                        else "query_failed",
                        "query_errors": query_errors,
                        "planner": planner_contract,
                        "original_query": plan_audit["original_query"],
                        "generated_variants": plan_audit["generated_variants"],
                        "normalized_variants": plan_audit["normalized_variants"],
                        "query_hashes": plan_audit["query_hashes"],
                        "plan_fingerprint": plan_audit["plan_fingerprint"],
                    },
                },
                requested=requested_profile,
                effective=effective_profile,
                failure_codes=[failure_code],
            )

        timing_rows: list[dict[str, Any]] = []
        for item, response in successful:
            timing = response.pop("_retrieval_timing", {})
            timing_rows.append({"query_id": item.query_id, **timing})

        from auth_context import get_search_limit

        final_budget = max(1, int(get_search_limit()))
        fusion_started = time.perf_counter()
        if len(successful) == 1:
            final_results = list(original_response.get("results", []))[:final_budget]
        else:
            final_results = multi_query_reciprocal_rank_fusion(
                [(item, response.get("results", [])) for item, response in successful],
                k=settings.rrf_k,
            )
            final_results = limit_chunks_per_document(
                final_results,
                max_chunks_per_document=settings.max_chunks_per_document,
                adaptive_max_chunks_per_document=settings.adaptive_max_chunks_per_document,
            )[:final_budget]
        global_fusion_seconds = time.perf_counter() - fusion_started

        total_memberships = sum(len(response.get("results", [])) for _, response in successful)
        unique_candidates = {
            hit_identity(item)
            for _, response in successful
            for item in response.get("results", [])
            if isinstance(item, dict)
        }
        duplicate_ratio = (
            (total_memberships - len(unique_candidates)) / total_memberships
            if total_memberships
            else 0.0
        )
        final_response = dict(original_response)
        final_response["results"] = final_results
        final_response["total"] = len(final_results)
        warnings = [
            warning
            for _, response in successful
            for warning in response.get("warnings", [])
            if isinstance(warning, dict)
        ]
        if generation_error:
            warnings.append(
                {
                    "code": "query_decomposition_unavailable",
                    "message": generation_error,
                }
            )
        if query_errors:
            warnings.append(
                {
                    "code": "derived_query_failed",
                    "queries": query_errors,
                    "message": "One or more derived queries failed under the same access scope.",
                }
            )
        if warnings:
            final_response["warnings"] = warnings
        multi_query_status = (
            "planner_failed" if generation_error else "query_failed" if query_errors else "success"
        )
        multi_query_executed = multi_query_status == "success"
        final_response["discovery"] = {
            "enabled": True,
            "multi_query_requested": True,
            "multi_query_executed": multi_query_executed,
            "multi_query_query_count": len(successful),
            "multi_query_status": multi_query_status,
            "query_count": len(successful),
            "generated_query_count": len(plan),
            "queries": [item.as_dict() for item in plan],
            "original_query": plan_audit["original_query"],
            "original_query_normalized": plan_audit["original_query_normalized"],
            "original_query_sha256": plan_audit["original_query_sha256"],
            "generated_variants": plan_audit["generated_variants"],
            "normalized_variants": plan_audit["normalized_variants"],
            "query_hashes": plan_audit["query_hashes"],
            "plan_fingerprint": plan_audit["plan_fingerprint"],
            "fusion": "hierarchical_rrf",
            "fusion_formula": "sum_q(1 / (rrf_k + per_query_rrf_rank))",
            "rrf_k": settings.rrf_k,
            "final_seed_chunk_budget": final_budget,
            "unique_seed_chunks": len(final_results),
            "unique_seed_documents": len(
                {
                    str(item.get("document_id"))
                    for item in final_results
                    if item.get("document_id") not in (None, "")
                }
            ),
            "duplicate_seed_ratio": duplicate_ratio,
            "query_errors": query_errors,
            "planner": planner_contract,
            "timings": {
                "query_generation_seconds": generation_seconds,
                "lexical_seconds_total": sum(
                    float(row.get("lexical_seconds", 0.0)) for row in timing_rows
                ),
                "dense_seconds_total": sum(
                    float(row.get("dense_seconds", 0.0)) for row in timing_rows
                ),
                "embedding_seconds_total": sum(
                    float(row.get("embedding_seconds", 0.0)) for row in timing_rows
                ),
                "per_query": timing_rows,
                "fusion_seconds": global_fusion_seconds
                + sum(float(row.get("fusion_seconds", 0.0)) for row in timing_rows),
                "retrieval_wall_seconds": time.perf_counter() - started,
            },
        }
        for lane in ("lexical", "dense", "fusion"):
            lane_rows = [
                response.get("effective_retrieval_profile", {}).get("lanes", {}).get(lane, {})
                for _, response in successful
            ]
            lane_rows = [row for row in lane_rows if isinstance(row, dict)]
            lane_execution = effective_profile["lanes"][lane]
            lane_execution["candidates"] = sum(int(row.get("candidates", 0)) for row in lane_rows)
            if (
                lane_rows
                and len(lane_rows) == len(plan)
                and all(row.get("status") == "succeeded" for row in lane_rows)
            ):
                lane_execution["status"] = "succeeded"
                lane_execution.pop("error", None)
            elif requested_profile["lanes"][lane] == "required":
                lane_execution.update({"status": "failed", "error": "query_lane_incomplete"})

        multi_query_execution = effective_profile["lanes"]["multi_query"]
        multi_query_execution.update(
            {
                "status": "succeeded" if multi_query_executed else "failed",
                "candidates": len(successful),
                "query_count": len(successful),
            }
        )
        if not multi_query_executed:
            multi_query_execution["error"] = multi_query_status
        else:
            multi_query_execution.pop("error", None)
        failure_codes: list[str] = []
        if generation_error:
            failure_codes.append("multi_query_planner_failed")
        if query_errors:
            failure_codes.append("multi_query_query_failed")
        return _finalize_retrieval_contract(
            final_response,
            requested=requested_profile,
            effective=effective_profile,
            failure_codes=failure_codes,
        )

    async def read_document_chunks(
        self,
        document_id: str,
        *,
        user_id: str,
        jwt_token: str | None,
        filters: dict[str, Any] | None = None,
        cursor: str = "",
        batch_size: int = 20,
    ) -> dict[str, Any]:
        """Read one immutable document snapshot in deterministic source order.

        This is the evidence path for exhaustive questions.  Unlike ranked
        search it does not discard low-scoring chunks.  The cursor is bound to
        the document content digest, so a replacement cannot silently mix two
        generations in one audit.
        """
        resolved_document_id = str(document_id or "").strip()
        if not resolved_document_id:
            raise ValueError("document_id is required for exhaustive retrieval")
        resolved_batch_size = min(EXHAUSTIVE_BATCH_MAX, max(1, int(batch_size)))
        scope_sha256 = exhaustive_scope_sha256(user_id=user_id, filters=filters)
        cursor_payload = decode_exhaustive_cursor(
            cursor,
            document_id=resolved_document_id,
            scope_sha256=scope_sha256,
        )
        snapshot_sha256 = cursor_payload.get("snapshot_sha256")
        covered_before = int(cursor_payload.get("covered_chunks", 0))

        filter_clauses: list[dict[str, Any]] = [
            {"term": {"document_id": resolved_document_id}},
            {"term": {"document_profile_version": EXHAUSTIVE_PROFILE_VERSION}},
            {"term": {"document_order_verified": True}},
        ]
        field_mapping = {
            "data_sources": "filename",
            "document_types": "mimetype",
            "owners": "owner",
            "connector_types": "connector_type",
        }
        for filter_key, values in (filters or {}).items():
            if values is None or not isinstance(values, list):
                continue
            field_name = field_mapping.get(filter_key, filter_key)
            if not values:
                filter_clauses.append({"term": {field_name: "__IMPOSSIBLE_VALUE__"}})
            elif len(values) == 1:
                filter_clauses.append({"term": {field_name: values[0]}})
            else:
                filter_clauses.append({"terms": {field_name: values}})
        if snapshot_sha256:
            filter_clauses.append({"term": {"document_content_sha256": snapshot_sha256}})

        pinned_profile = cursor_payload.get("document_profile")
        if isinstance(pinned_profile, dict):
            for field in ("ingest_run_id", "source_entity_id", "occurrence_id"):
                if pinned_profile.get(field) is not None:
                    filter_clauses.append({"term": {field: pinned_profile[field]}})

        source_fields = [
            "document_profile_version",
            "ingest_run_id",
            "occurrence_id",
            "chunk_id",
            "chunk_content_sha256",
            "document_id",
            "document_content_sha256",
            "document_chunk_count",
            "document_page_count",
            "document_max_page",
            "document_character_count",
            "document_size_class",
            "document_order_verified",
            "filename",
            "mimetype",
            "page",
            "chunk_index",
            "chunking_strategy",
            "text",
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
            "connector_file_id",
            "owner",
            "owner_name",
            "owner_email",
            "file_size",
            "connector_type",
            "embedding_model",
            "embedding_dimensions",
            "parser",
            "chunk_size",
            "chunk_overlap",
        ]
        body: dict[str, Any] = {
            "query": {"bool": {"filter": filter_clauses}},
            "_source": source_fields,
            "size": resolved_batch_size,
            "track_total_hits": True,
            "sort": [
                {"chunk_index": {"order": "asc", "missing": "_last"}},
                {"page": {"order": "asc", "missing": "_last"}},
                {"chunk_id": {"order": "asc"}},
            ],
            "aggs": {"snapshots": {"terms": {"field": "document_content_sha256", "size": 2}}},
        }
        if cursor_payload:
            body["search_after"] = cursor_payload["search_after"]

        client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)
        response = await client.search(
            index=get_index_name(), body=body, params={"terminate_after": 0}
        )
        failures = validate_search_response(response, exact_total=True)
        raw_hits = response.get("hits", {}).get("hits", [])
        raw_hits = raw_hits if isinstance(raw_hits, list) else []
        current_total = response.get("hits", {}).get("total")
        current_total = (
            current_total.get("value") if isinstance(current_total, dict) else current_total
        )
        chunks: list[dict[str, Any]] = []
        profile_fields = (
            "document_id",
            "document_profile_version",
            "document_order_verified",
            "document_content_sha256",
            "document_chunk_count",
            "ingest_run_id",
            "source_entity_id",
            "occurrence_id",
            "owner",
            "filename",
        )
        profile = pinned_profile
        manifest = cursor_payload.get("verified_manifest", [])
        if not isinstance(manifest, list):
            manifest = []
            failures.append("cursor_invalid: missing verified chunk manifest")
        if cursor_payload and (not isinstance(profile, dict) or len(manifest) != covered_before):
            failures.append("cursor_invalid: missing immutable document profile")
        previous_sort = cursor_payload.get("search_after")
        try:
            if raw_hits and profile is None:
                source = raw_hits[0].get("_source", {})
                profile = {key: source.get(key) for key in profile_fields}
            if not isinstance(profile, dict):
                raise ValueError("The document has no verifiable ingestion profile")
            expected_count = profile.get("document_chunk_count")
            snapshot_sha256 = profile.get("document_content_sha256")
            if (
                profile.get("document_id") != resolved_document_id
                or profile.get("document_profile_version") != EXHAUSTIVE_PROFILE_VERSION
                or profile.get("document_order_verified") is not True
                or type(expected_count) is not int
                or expected_count <= 0
                or not isinstance(profile.get("ingest_run_id"), str)
                or not profile["ingest_run_id"]
                or not isinstance(snapshot_sha256, str)
                or len(snapshot_sha256) != 64
            ):
                raise ValueError("Document verification profile is invalid")
            snapshots = response.get("aggregations", {}).get("snapshots", {}).get("buckets", [])
            if len(snapshots) != 1 or snapshots[0].get("key") != snapshot_sha256:
                failures.append("snapshot_changed: document snapshot cannot be proven stable")
            if current_total != expected_count:
                failures.append("Document evidence count does not match its profile")
            if len(raw_hits) > resolved_batch_size:
                failures.append("Document page exceeded requested size")
            for hit in raw_hits:
                source = hit.get("_source", {})
                if {key: source.get(key) for key in profile_fields} != profile:
                    raise ValueError(
                        "snapshot_changed: document identity, generation or profile changed"
                    )
                if source.get("chunk_index") != len(manifest):
                    raise ValueError(
                        "Exhaustive retrieval encountered a non-contiguous source order"
                    )
                validate_search_progress(previous_sort, hit.get("sort"), width=3)
                entry = verified_chunk_manifest(source)
                if entry["chunk_id"] in {item.get("chunk_id") for item in manifest}:
                    raise ValueError("Document evidence contains a duplicate chunk identity")
                manifest.append(entry)
                previous_sort = hit["sort"]
                chunks.append(
                    {**source, "id": hit.get("_id"), "score": None, "evidence_order": len(manifest)}
                )
            # Validate the prefix even before final digest verification.
            document_manifest_sha256(manifest)
            if len(manifest) > expected_count:
                raise ValueError("Exhaustive retrieval coverage exceeded snapshot size")
            if len(manifest) == expected_count:
                verify_complete_document(
                    manifest, expected_count=expected_count, expected_snapshot=snapshot_sha256
                )
            elif not raw_hits or len(raw_hits) < resolved_batch_size:
                raise ValueError("Exhaustive retrieval stopped before complete coverage")
        except (ValueError, TypeError, KeyError) as exc:
            failures.append(str(exc))
        profile = profile if isinstance(profile, dict) else {}
        total_chunks = profile.get("document_chunk_count")
        total_chunks = total_chunks if type(total_chunks) is int and total_chunks > 0 else 0
        covered_chunks = len(manifest)
        complete = not failures and covered_chunks == total_chunks and total_chunks > 0
        next_cursor = None
        if not failures and not complete:
            next_cursor = encode_exhaustive_cursor(
                document_id=resolved_document_id,
                snapshot_sha256=snapshot_sha256,
                search_after=previous_sort,
                covered_chunks=covered_chunks,
                scope_sha256=scope_sha256,
                document_profile=profile,
                verified_manifest=manifest,
            )
        coverage = {
            "mode": "exhaustive",
            "document_id": resolved_document_id,
            "filename": profile.get("filename"),
            "snapshot_sha256": snapshot_sha256,
            "ingest_run_id": profile.get("ingest_run_id"),
            "covered_chunks": covered_chunks,
            "total_chunks": total_chunks,
            "coverage_ratio": covered_chunks / total_chunks if total_chunks else 0.0,
            "complete": complete,
            "retrieval_execution_complete": not validate_search_response(
                response, exact_total=True
            ),
            "failure_codes": sorted(set(failures)),
            "next_cursor": next_cursor,
        }
        return {
            "results": chunks,
            "total": len(chunks),
            "coverage": coverage,
            **({"error": "; ".join(sorted(set(failures)))} if failures else {}),
        }

    @staticmethod
    def _scope_seed_manifest(result: dict[str, Any]) -> dict[str, Any]:
        """Keep one compact, human-readable representative per seed document."""
        manifest = {
            field: result.get(field)
            for field in (
                "document_id",
                "filename",
                "mimetype",
                "source_url",
                "connector_file_id",
                "owner",
                "source_provenance",
                "source_entity_id",
                "source_entity_type",
                "source_entity_system",
                "source_entity_alternate_ids",
                "source_relative_path",
                "source_path_ancestors",
            )
            if result.get(field) not in (None, "", [], {})
        }
        provenance = result.get("source_provenance")
        entity = provenance.get("entity") if isinstance(provenance, dict) else None
        generated_at = entity.get("generated_at_time") if isinstance(entity, dict) else None
        if generated_at not in (None, ""):
            manifest["generated_at_time"] = generated_at
        return manifest

    @staticmethod
    def _scope_filter_clauses(filters: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Translate public knowledge filters for provenance graph queries."""
        field_mapping = {
            "data_sources": "filename",
            "document_types": "mimetype",
            "owners": "owner",
            "connector_types": "connector_type",
        }
        clauses: list[dict[str, Any]] = []
        for filter_key, values in (filters or {}).items():
            if not isinstance(values, list):
                continue
            field_name = field_mapping.get(filter_key, filter_key)
            if not values:
                clauses.append({"term": {field_name: "__IMPOSSIBLE_VALUE__"}})
            elif len(values) == 1:
                clauses.append({"term": {field_name: values[0]}})
            else:
                clauses.append({"terms": {field_name: values}})
        return clauses

    @staticmethod
    def _scope_document_sort_key(document: dict[str, Any]) -> tuple[str, str, str, str]:
        """Prefer an asserted source date, then stable human/id tie-breakers."""
        generated_at = document.get("generated_at_time")
        provenance = document.get("source_provenance")
        if generated_at in (None, "") and isinstance(provenance, dict):
            entity = provenance.get("entity")
            if isinstance(entity, dict):
                generated_at = entity.get("generated_at_time")
        return (
            str(generated_at or "9999-12-31T23:59:59Z"),
            str(document.get("filename") or ""),
            str(document.get("document_id") or ""),
            str(document.get("source_entity_id") or ""),
        )

    @staticmethod
    def _scope_document_failure_code(error: str) -> str:
        """Classify document-read failures without leaking unstable exceptions."""
        normalized = str(error or "").casefold()
        if any(token in normalized for token in ("forbidden", "unauthorized", "access denied")):
            return "access_error"
        if "401" in normalized or "403" in normalized:
            return "access_error"
        if "cursor" in normalized:
            return "cursor_invalid"
        if "digest" in normalized:
            return "profile_invalid"
        if "snapshot" in normalized or "document changed" in normalized:
            return "snapshot_changed"
        if "no verifiable ingestion profile" in normalized or "reindex it" in normalized:
            return "legacy_document"
        if any(
            token in normalized
            for token in (
                "profile",
                "non-contiguous",
                "unverified",
                "unverifiable",
                "coverage exceeded",
                "exact snapshot chunk count",
                "mixed document filenames",
            )
        ):
            return "profile_invalid"
        return "document_read_incomplete"

    @staticmethod
    def _scope_error_coverage(query: str, code: str, error: str) -> dict[str, Any]:
        """Build the same fail-closed certificate for an early search failure."""
        decision = certify_scope_coverage(
            ScopeCertificationFacts(
                seed_discovery_complete=False,
                seed_documents=0,
                valid_provenance_seed_documents=0,
                invalid_provenance_seed_documents=0,
                graph_frontier_empty=False,
                graph_limit_reached=False,
                graph_stop_reason=None,
                graph_failed=False,
                retrieval_execution_complete=False,
                documents_discovered=0,
                documents_complete=0,
                covered_chunks=0,
                total_chunks=0,
                seed_failure_code=code,
            )
        )
        return {
            "mode": "scope_exhaustive",
            "query": query,
            "scope_policy_id": DEFAULT_SCOPE_TRAVERSAL_POLICY.policy_id,
            "scope_policy_version": DEFAULT_SCOPE_TRAVERSAL_POLICY.version,
            "seed_discovery_complete": False,
            "seed_documents": 0,
            "valid_provenance_seed_documents": 0,
            "invalid_provenance_seed_documents": 0,
            "retrieval_execution_complete": False,
            "retrieval_failure_codes": [],
            "graph_frontier_empty": False,
            "graph_limit_reached": False,
            "graph_stop_reason": None,
            "graph_failed": False,
            "documents_discovered": 0,
            "documents_complete": 0,
            "covered_chunks": 0,
            "total_chunks": 0,
            "document_read_coverage_ratio": 0.0,
            "coverage_ratio": 0.0,
            "relations_traversed": {"total": 0, "by_classification": []},
            "relations_context_only": {"total": 0, "by_classification": []},
            "relations_excluded_by_policy": {"total": 0, "by_classification": []},
            "relations_unclassified": {"total": 0, "by_classification": []},
            "error": error,
            **decision,
            "stop_reason": decision["status_code"],
        }

    async def search_exhaustive_scope(
        self,
        query: str,
        *,
        user_id: str,
        jwt_token: str | None,
        filters: dict[str, Any] | None,
        embedding_model: str | None,
        settings: ScopeExhaustiveSettings,
        multi_query_discovery: bool = False,
        multi_query_max_queries: int = MAX_DISCOVERY_QUERIES,
        multi_query_concurrency: int = 2,
        metadata_restriction: MetadataCandidateRestriction | None = None,
    ) -> dict[str, Any]:
        """Discover, close and verify one query-defined documentary scope.

        Ranked Retrieval v2 supplies broad seeds. The user's DLS-scoped client
        then closes the accessible PROV-O graph in both directions. Every
        discovered document is finally read with ``read_document_chunks``;
        ranked chunks are never mistaken for proof of complete reading.
        """
        from auth_context import set_score_threshold, set_search_limit

        scope_started = time.perf_counter()
        set_search_limit(settings.seed_count)
        set_score_threshold(0)
        try:
            if multi_query_discovery:
                seed_response = await self._search_multi_query(
                    query,
                    embedding_model=embedding_model,
                    max_queries=multi_query_max_queries,
                    concurrency=multi_query_concurrency,
                    metadata_restriction=metadata_restriction,
                )
            else:
                seed_response = await self.search_tool(
                    query,
                    embedding_model=embedding_model,
                    _metadata_restriction=metadata_restriction,
                )
        except Exception as exc:
            logger.warning("Scope seed discovery failed", error=str(exc))
            return {
                "results": [],
                "documents": [],
                "graph": {"entities": [], "edges": []},
                "error": f"Seed discovery failed: {exc}",
                "coverage": self._scope_error_coverage(
                    query, "search_error", f"Seed discovery failed: {exc}"
                ),
            }

        if seed_response.get("error"):
            failure_coverage = self._scope_error_coverage(
                query, "incomplete_seed_discovery", str(seed_response["error"])
            )
            for field in (
                "requested_retrieval_profile",
                "effective_retrieval_profile",
                "retrieval_execution_complete",
                "retrieval_failure_codes",
            ):
                if field in seed_response:
                    failure_coverage[field] = seed_response[field]
            # Re-certify the actual execution facts after their transport
            # projection; never retain a decision over the pre-projection facts.
            failure_facts = dict(failure_coverage["certification"]["facts"])
            failure_facts["retrieval_execution_complete"] = failure_coverage[
                "retrieval_execution_complete"
            ]
            failure_facts["retrieval_failure_codes"] = tuple(
                failure_coverage["retrieval_failure_codes"]
            )
            failure_coverage.update(
                certify_scope_coverage(ScopeCertificationFacts(**failure_facts))
            )
            return {
                "results": [],
                "documents": [],
                "graph": {"entities": [], "edges": []},
                "error": seed_response["error"],
                "coverage": failure_coverage,
                **(
                    {"warnings": seed_response["warnings"]}
                    if isinstance(seed_response.get("warnings"), list)
                    else {}
                ),
            }

        seed_results = [item for item in seed_response.get("results", []) if isinstance(item, dict)]
        seed_documents: dict[str, dict[str, Any]] = {}
        seed_entities: set[str] = set()
        seed_primary_entities: set[str] = set()
        seed_provenance_validity: dict[str, bool] = {}
        seed_provenance_failures: list[dict[str, str]] = []
        seed_entity_ids_by_document: dict[str, set[str]] = {}
        for result in seed_results:
            document_id = result.get("document_id")
            if not isinstance(document_id, str) or not document_id.strip():
                continue
            normalized_document_id = document_id.strip()
            seed_documents.setdefault(normalized_document_id, self._scope_seed_manifest(result))
            try:
                provenance = validate_provenance_representative(result)
            except ValueError as exc:
                seed_provenance_failures.append(
                    {"document_id": normalized_document_id, "reason": str(exc)}
                )
                provenance = None
            observation_valid = provenance is not None
            if provenance is not None:
                seed_documents[normalized_document_id].update(provenance.index_fields())
            seed_provenance_validity[normalized_document_id] = (
                seed_provenance_validity.get(normalized_document_id, True) and observation_valid
            )
            if observation_valid and provenance is not None:
                entity_ids = {
                    provenance.entity.id,
                    *provenance.entity.alternate_ids,
                }
                seed_entity_ids_by_document.setdefault(normalized_document_id, set()).update(
                    entity_ids
                )
                seed_primary_entities.add(provenance.entity.id)

        valid_provenance_seed_documents = {
            document_id for document_id, valid in seed_provenance_validity.items() if valid
        }
        for document_id in valid_provenance_seed_documents:
            seed_entities.update(seed_entity_ids_by_document.get(document_id, set()))
        invalid_provenance_seed_documents = set(seed_documents) - (valid_provenance_seed_documents)

        user_client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)
        hidden_resolver = getattr(self, "_provenance_hidden_targets", None)
        graph_failed = False
        try:
            graph = await expand_provenance_graph(
                user_client,
                index_name=get_index_name(),
                seed_entity_ids=seed_primary_entities,
                seed_documents=seed_documents.values(),
                policy=DEFAULT_SCOPE_TRAVERSAL_POLICY,
                max_depth=settings.max_depth,
                max_entities=settings.max_entities,
                max_documents=settings.max_documents,
                filter_clauses=self._scope_filter_clauses(filters),
                hidden_target_resolver=(
                    (lambda targets: hidden_resolver(user_client, targets))
                    if hidden_resolver
                    else None
                ),
            )
        except Exception as exc:
            logger.warning("Scope provenance traversal failed", error=str(exc))
            graph_failed = True
            graph = {
                "documents": [seed_documents[key] for key in sorted(seed_documents)],
                "entities": [],
                "edges": [],
                "context_edges": [],
                "coverage": {
                    "scope_policy_id": DEFAULT_SCOPE_TRAVERSAL_POLICY.policy_id,
                    "scope_policy_version": DEFAULT_SCOPE_TRAVERSAL_POLICY.version,
                    "entities_visited": 0,
                    "documents_discovered": len(seed_documents),
                    "depth_reached": 0,
                    "frontier_empty": False,
                    "limit_reached": False,
                    "stop_reason": "graph_traversal_failed",
                    "error": str(exc),
                    "relations_traversed": {"total": 0, "by_classification": []},
                    "relations_context_only": {"total": 0, "by_classification": []},
                    "relations_excluded_by_policy": {
                        "total": 0,
                        "by_classification": [],
                    },
                    "relations_unclassified": {"total": 0, "by_classification": []},
                },
            }

        documents = sorted(graph["documents"], key=self._scope_document_sort_key)
        evidence: list[dict[str, Any]] = []
        document_manifest: list[dict[str, Any]] = []
        evidence_batches: list[dict[str, Any]] = []
        covered_chunks = 0
        total_chunks = 0
        complete_documents = 0
        document_failure_codes: list[str] = []

        for document in documents:
            document_id = str(document.get("document_id") or "")
            # A content-derived document_id can have several legitimate source
            # occurrences (for example a local file and the same attachment in
            # OpenArchiver). Read and certify the exact PROV-O occurrence chosen
            # by graph closure. A later ingest of that same occurrence still
            # shares this filter and therefore remains subject to the existing
            # snapshot_changed protection.
            document_filters = copy.deepcopy(filters or {})
            source_entity_id = document.get("source_entity_id")
            if isinstance(source_entity_id, str) and source_entity_id.strip():
                requested_entities = document_filters.get("source_entity_id")
                if isinstance(requested_entities, list) and requested_entities:
                    document_filters["source_entity_id"] = (
                        [source_entity_id] if source_entity_id in requested_entities else []
                    )
                else:
                    document_filters["source_entity_id"] = [source_entity_id]
            document_evidence: list[dict[str, Any]] = []
            cursor = ""
            seen_cursors: set[str] = set()
            final_coverage: dict[str, Any] = {
                "mode": "exhaustive",
                "document_id": document_id,
                "complete": False,
            }
            read_error: str | None = None
            try:
                while True:
                    page = await self.read_document_chunks(
                        document_id,
                        user_id=user_id,
                        jwt_token=jwt_token,
                        filters=document_filters,
                        cursor=cursor,
                        batch_size=settings.batch_size,
                    )
                    document_evidence.extend(page.get("results", []))
                    page_coverage = page.get("coverage")
                    if isinstance(page_coverage, dict):
                        final_coverage = page_coverage
                    if page.get("error"):
                        read_error = str(page["error"])
                        break
                    if final_coverage.get("complete") is True:
                        break
                    next_cursor = final_coverage.get("next_cursor")
                    if not isinstance(next_cursor, str) or not next_cursor:
                        read_error = "Document read stopped without a continuation cursor"
                        break
                    if next_cursor in seen_cursors:
                        read_error = "Document read returned a repeated continuation cursor"
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
            except Exception as exc:
                read_error = str(exc)

            if read_error is None and final_coverage.get("complete") is True:
                try:
                    verify_complete_document(
                        [verified_chunk_manifest(chunk) for chunk in document_evidence],
                        expected_count=final_coverage.get("total_chunks"),
                        expected_snapshot=final_coverage.get("snapshot_sha256"),
                    )
                except (TypeError, ValueError) as exc:
                    read_error = str(exc)

            evidence.extend(document_evidence)
            document_covered = int(final_coverage.get("covered_chunks", len(document_evidence)))
            document_total = int(final_coverage.get("total_chunks", document_covered))
            covered_chunks += document_covered
            total_chunks += document_total
            document_complete = final_coverage.get("complete") is True and read_error is None
            if document_complete:
                complete_documents += 1
                document_status_code = "complete"
            else:
                document_status_code = self._scope_document_failure_code(
                    read_error or "Document read did not report complete coverage"
                )
                document_failure_codes.append(document_status_code)
            manifest_item = {
                **document,
                "coverage": final_coverage,
                "complete": document_complete,
                "status_code": document_status_code,
            }
            if read_error:
                manifest_item["error"] = read_error
            document_manifest.append(manifest_item)
            evidence_batches.append(
                {
                    "document_id": document_id,
                    "filename": final_coverage.get("filename") or document.get("filename"),
                    "chunk_ids": [
                        item.get("chunk_id") for item in document_evidence if item.get("chunk_id")
                    ],
                    "complete": document_complete,
                    "status_code": document_status_code,
                }
            )

        graph_coverage = graph["coverage"]
        graph_failed = graph_failed or graph_coverage.get("execution_complete") is False
        seed_provenance_complete = bool(seed_documents) and not (invalid_provenance_seed_documents)
        incomplete_documents = len(documents) - complete_documents
        decision = certify_scope_coverage(
            ScopeCertificationFacts(
                seed_discovery_complete=True,
                seed_documents=len(seed_documents),
                valid_provenance_seed_documents=len(valid_provenance_seed_documents),
                invalid_provenance_seed_documents=len(invalid_provenance_seed_documents),
                graph_frontier_empty=graph_coverage.get("frontier_empty") is True,
                graph_limit_reached=graph_coverage.get("limit_reached") is True,
                graph_stop_reason=graph_coverage.get("stop_reason"),
                graph_failed=graph_failed,
                retrieval_execution_complete=(
                    seed_response.get("retrieval_execution_complete") is True
                ),
                documents_discovered=len(documents),
                documents_complete=complete_documents,
                covered_chunks=covered_chunks,
                total_chunks=total_chunks,
                document_failure_codes=tuple(document_failure_codes),
                retrieval_failure_codes=tuple(
                    str(code)
                    for code in seed_response.get("retrieval_failure_codes", [])
                    if isinstance(code, str)
                ),
                unclassified_relations=int(
                    graph_coverage.get("relations_unclassified", {}).get("total", 0)
                ),
            )
        )
        coverage = {
            "mode": "scope_exhaustive",
            "query": query,
            "scope_policy_id": graph_coverage.get(
                "scope_policy_id", DEFAULT_SCOPE_TRAVERSAL_POLICY.policy_id
            ),
            "scope_policy_version": graph_coverage.get(
                "scope_policy_version", DEFAULT_SCOPE_TRAVERSAL_POLICY.version
            ),
            "seed_discovery_complete": True,
            "seed_documents": len(seed_documents),
            "seed_entities": len(seed_entities),
            "valid_provenance_seed_documents": len(valid_provenance_seed_documents),
            "invalid_provenance_seed_documents": len(invalid_provenance_seed_documents),
            "seed_provenance_complete": seed_provenance_complete,
            "seed_provenance_failures": seed_provenance_failures,
            "requested_retrieval_profile": seed_response.get("requested_retrieval_profile"),
            "effective_retrieval_profile": seed_response.get("effective_retrieval_profile"),
            "retrieval_execution_complete": (
                seed_response.get("retrieval_execution_complete") is True
            ),
            "retrieval_failure_codes": [
                str(code)
                for code in seed_response.get("retrieval_failure_codes", [])
                if isinstance(code, str)
            ],
            "graph_entities_visited": graph_coverage.get("entities_visited", 0),
            "graph_frontier_empty": graph_coverage.get("frontier_empty", False),
            "graph_limit_reached": graph_coverage.get("limit_reached", False),
            "graph_stop_reason": graph_coverage.get("stop_reason"),
            "graph_failed": graph_failed,
            "graph_error": graph_coverage.get("error"),
            "graph_execution_complete": not graph_failed,
            "graph_execution_failure_codes": graph_coverage.get("execution_failure_codes", []),
            "provenance_failures": graph_coverage.get("provenance_failures", []),
            "graph_forward_hits": graph_coverage.get("forward_hits", 0),
            "graph_reverse_hits": graph_coverage.get("reverse_hits", 0),
            "graph_forward_pages": graph_coverage.get("forward_pages", 0),
            "graph_reverse_pages": graph_coverage.get("reverse_pages", 0),
            "graph_forward_verification_pages": graph_coverage.get("forward_verification_pages", 0),
            "graph_reverse_verification_pages": graph_coverage.get("reverse_verification_pages", 0),
            "graph_distinct_results": graph_coverage.get("distinct_results", 0),
            "graph_stability_verified": graph_coverage.get("stability_verified", False),
            "graph_stability_observations": graph_coverage.get("stability_observations", 0),
            "relations_traversed": graph_coverage.get(
                "relations_traversed", {"total": 0, "by_classification": []}
            ),
            "relations_context_only": graph_coverage.get(
                "relations_context_only", {"total": 0, "by_classification": []}
            ),
            "relations_excluded_by_policy": graph_coverage.get(
                "relations_excluded_by_policy", {"total": 0, "by_classification": []}
            ),
            "relations_unclassified": graph_coverage.get(
                "relations_unclassified", {"total": 0, "by_classification": []}
            ),
            "identity_shared_aliases_resolved": graph_coverage.get(
                "identity_shared_aliases_resolved", 0
            ),
            "scope_diagnostics": graph_coverage.get(
                "scope_diagnostics",
                {
                    "documents_per_depth": [],
                    "entities_per_depth": [],
                    "relations_per_depth": [],
                    "largest_expansion_contributors": [],
                },
            ),
            "documents_discovered": len(documents),
            "documents_complete": complete_documents,
            "documents_incomplete": incomplete_documents,
            "covered_chunks": covered_chunks,
            "total_chunks": total_chunks,
            "document_read_coverage_ratio": (
                1.0
                if decision["complete"] and total_chunks == 0
                else covered_chunks / total_chunks
                if total_chunks
                else 0.0
            ),
            # Kept as a compatibility alias. It measures only document reads
            # and is never an independent proof of scope closure.
            "coverage_ratio": (
                1.0
                if decision["complete"] and total_chunks == 0
                else covered_chunks / total_chunks
                if total_chunks
                else 0.0
            ),
            **decision,
            "stop_reason": decision["status_code"],
        }
        discovery_metadata = seed_response.get("discovery")
        discovery_wall_seconds = 0.0
        if isinstance(discovery_metadata, dict):
            timings = discovery_metadata.get("timings")
            if isinstance(timings, dict):
                discovery_wall_seconds = float(timings.get("retrieval_wall_seconds", 0.0))
            coverage["discovery"] = discovery_metadata
        total_scope_seconds = time.perf_counter() - scope_started
        coverage["performance"] = {
            "discovery_seconds": discovery_wall_seconds,
            "prov_o_seconds": max(0.0, total_scope_seconds - discovery_wall_seconds),
            "total_seconds": total_scope_seconds,
        }
        # Langflow retains every verified chunk in its artifact for source
        # cards/UI. Its model-facing projection uses this bounded leaf-evidence
        # set: ranked seeds first, then one source-order chunk for each newly
        # linked document. No generated summary is allowed to stand in as
        # evidence, and the projection size is disclosed in coverage.
        model_evidence_by_id: dict[str, dict[str, Any]] = {}
        represented_documents: set[str] = set()
        for item in seed_results:
            chunk_id = item.get("chunk_id")
            if isinstance(chunk_id, str):
                model_evidence_by_id.setdefault(chunk_id, item)
                document_id = item.get("document_id")
                if isinstance(document_id, str):
                    represented_documents.add(document_id)
        for item in evidence:
            document_id = item.get("document_id")
            if isinstance(document_id, str) and document_id in represented_documents:
                continue
            chunk_id = item.get("chunk_id")
            if isinstance(chunk_id, str):
                model_evidence_by_id.setdefault(chunk_id, item)
            if isinstance(document_id, str):
                represented_documents.add(document_id)
            if len(model_evidence_by_id) >= settings.seed_count:
                break
        model_results = list(model_evidence_by_id.values())[: settings.seed_count]
        coverage["model_evidence_chunks"] = len(model_results)
        coverage["artifact_chunks"] = len(evidence)
        response = {
            "results": evidence,
            "model_results": model_results,
            "total": len(evidence),
            "requested_retrieval_profile": seed_response.get("requested_retrieval_profile"),
            "effective_retrieval_profile": seed_response.get("effective_retrieval_profile"),
            "retrieval_execution_complete": (
                seed_response.get("retrieval_execution_complete") is True
            ),
            "retrieval_failure_codes": [
                str(code)
                for code in seed_response.get("retrieval_failure_codes", [])
                if isinstance(code, str)
            ],
            "documents": document_manifest,
            "evidence_batches": evidence_batches,
            "graph": {
                "entities": graph["entities"],
                "edges": graph["edges"],
                "context_edges": graph.get("context_edges", []),
            },
            "coverage": coverage,
        }
        if isinstance(discovery_metadata, dict):
            response["discovery"] = discovery_metadata
        if isinstance(seed_response.get("metadata_filter"), dict):
            response["metadata_filter"] = seed_response["metadata_filter"]
            coverage["metadata_filter"] = seed_response["metadata_filter"]
        if isinstance(seed_response.get("retrieval_diagnostics"), dict):
            response["retrieval_diagnostics"] = seed_response["retrieval_diagnostics"]
        if isinstance(seed_response.get("warnings"), list):
            response["warnings"] = seed_response["warnings"]
        return response

    async def search(
        self,
        query: str,
        user_id: str = None,
        jwt_token: str = None,
        filters: dict[str, Any] = None,
        limit: int = 10,
        score_threshold: float = 0,
        embedding_model: str = None,
        evidence_mode: str = "focused",
        document_id: str | None = None,
        cursor: str = "",
        batch_size: int = 20,
        group_by_document: bool = False,
        page: int = 1,
        page_size: int = 100,
        multi_query_discovery: bool = False,
        multi_query_max_queries: int = MAX_DISCOVERY_QUERIES,
        multi_query_concurrency: int = 2,
        metadata_filter: MetadataFilter | None = None,
    ) -> dict[str, Any]:
        """Public search method for API endpoints

        Args:
            embedding_model: Embedding model to use for search (defaults to the
                currently configured embedding model)
        """
        if evidence_mode not in {"focused", "exhaustive", "scope_exhaustive"}:
            raise ValueError("evidence_mode must be 'focused', 'exhaustive', or 'scope_exhaustive'")
        if metadata_filter is not None and evidence_mode == "exhaustive":
            raise ValueError(
                "metadata_filter is not supported for direct exhaustive document reads"
            )
        if metadata_filter is not None and group_by_document:
            raise ValueError("metadata_filter is not supported for paginated document browsing")
        if metadata_filter is not None and not query.strip():
            raise ValueError("free_text is required when metadata_filter is provided")

        # Set auth context if provided (for direct API calls)
        from config.settings import is_no_auth_mode

        if user_id and (jwt_token or is_no_auth_mode()):
            from auth_context import set_auth_context

            set_auth_context(user_id, jwt_token)

        # Set filters and limit in context if provided
        if filters:
            from auth_context import set_search_filters

            set_search_filters(filters)

        metadata_restriction: MetadataCandidateRestriction | None = None
        if metadata_filter is not None:
            if not user_id:
                return {"results": [], "error": "Authentication required"}
            dls_client = self.session_manager.get_user_opensearch_client(user_id, jwt_token)
            metadata_restriction = await resolve_metadata_candidates(
                dls_client,
                metadata_filter,
            )

        resolved_query = query if query.strip() else "*"

        if evidence_mode == "exhaustive":
            if not user_id:
                return {"results": [], "error": "Authentication required"}
            result = await self.read_document_chunks(
                document_id or "",
                user_id=user_id,
                jwt_token=jwt_token,
                filters=filters,
                cursor=cursor,
                batch_size=batch_size,
            )
            return redact_dls_opaque_relation_metadata(result)

        if evidence_mode == "scope_exhaustive":
            if not user_id:
                return {"results": [], "error": "Authentication required"}
            if not query.strip():
                raise ValueError("query is required for scope_exhaustive retrieval")
            settings = ScopeExhaustiveSettings.from_knowledge(get_openrag_config().knowledge)
            result = await self.search_exhaustive_scope(
                resolved_query,
                user_id=user_id,
                jwt_token=jwt_token,
                filters=filters,
                embedding_model=embedding_model,
                settings=settings,
                multi_query_discovery=multi_query_discovery,
                multi_query_max_queries=multi_query_max_queries,
                multi_query_concurrency=multi_query_concurrency,
                metadata_restriction=metadata_restriction,
            )
            return redact_dls_opaque_relation_metadata(result)

        from auth_context import set_score_threshold, set_search_limit

        set_search_limit(limit)
        set_score_threshold(score_threshold)

        if multi_query_discovery:
            if group_by_document:
                raise ValueError("multi-query discovery is not available for document browsing")
            result = await self._search_multi_query(
                resolved_query,
                embedding_model=embedding_model,
                max_queries=multi_query_max_queries,
                concurrency=multi_query_concurrency,
                metadata_restriction=metadata_restriction,
            )
        else:
            search_kwargs: dict[str, Any] = {
                "embedding_model": embedding_model,
                "group_by_document": group_by_document,
                "page": page,
                "page_size": page_size,
            }
            if metadata_restriction is not None:
                search_kwargs["_metadata_restriction"] = metadata_restriction
            result = await self.search_tool(resolved_query, **search_kwargs)
        return redact_dls_opaque_relation_metadata(result)
