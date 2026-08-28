import asyncio
import copy
import hashlib
import hmac
import os
import re
from typing import Any

from agentd.tool_decorator import tool

from auth_context import get_auth_context
from config.embedding_constants import get_declared_default_embedding_model
from config.settings import clients, get_embedding_model, get_index_name, get_openrag_config
from services.retrieval_service import (
    EXHAUSTIVE_BATCH_MAX,
    EXHAUSTIVE_PROFILE_VERSION,
    HttpReranker,
    RetrievalSettings,
    decode_exhaustive_cursor,
    encode_exhaustive_cursor,
    exhaustive_scope_sha256,
    limit_chunks_per_document,
    reciprocal_rank_fusion,
)
from utils.container_utils import transform_localhost_url
from utils.logging_config import get_logger
from utils.rrf_mapping import RRFMappingError, require_sortable_chunk_id_mapping

logger = get_logger(__name__)

MAX_EMBED_RETRIES = 3
EMBED_RETRY_INITIAL_DELAY = 1.0
EMBED_RETRY_MAX_DELAY = 8.0
DOCUMENT_SEARCH_RESULT_WINDOW = 10_000


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
    return await _global_search_service.search_tool(query, embedding_model=embedding_model)


class SearchService:
    def __init__(self, session_manager=None, models_service=None):
        self.session_manager = session_manager
        self.models_service = models_service
        self._configure_provider_env()

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

        return [by_cited_identity[item] for item in ordered_ids if item in by_cited_identity]

    async def search_tool(
        self,
        query: str,
        embedding_model: str = None,
        *,
        group_by_document: bool = False,
        page: int = 1,
        page_size: int = 100,
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
                return {"results": [], "error": str(exc), "retrieval_strategy": "rrf"}

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
            embedding_results = await asyncio.gather(
                *[embed_with_model(model) for model in available_models],
                return_exceptions=True,
            )

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
                # A fixed candidate horizon keeps both page membership and the
                # document cardinality stable while the user moves between
                # pages. Growing k with the requested page would make the
                # displayed total change after every click.
                knn_result_count = (
                    DOCUMENT_SEARCH_RESULT_WINDOW if group_by_document else 50
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

        async def execute_search(body: dict[str, Any], label: str) -> dict[str, Any]:
            fallback_body = without_num_candidates(body)
            try:
                index_name = get_index_name()
                logger.info("Sending query to index", retrieval_lane=label, index_name=index_name)
                return await opensearch_client.search(
                    index=index_name, body=body, params=search_params
                )
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
                        return await opensearch_client.search(
                            index=get_index_name(), body=fallback_body, params=search_params
                        )
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

        retrieval_results: dict[str, dict[str, Any]] = {}
        if retrieval_bodies:
            lanes = [name for name, _body in retrieval_bodies]
            lane_results = await asyncio.gather(
                *[execute_search(body, name) for name, body in retrieval_bodies]
            )
            retrieval_results = dict(zip(lanes, lane_results, strict=True))
            results = retrieval_results.get("lexical") or lane_results[0]
        else:
            results = await execute_search(search_body, "weighted")

        raw_hits = results.get("hits", {}).get("hits", [])
        if retrieval_results:
            ranked_lists = [
                lane_result.get("hits", {}).get("hits", [])
                for lane_result in retrieval_results.values()
            ]
            raw_hits = reciprocal_rank_fusion(ranked_lists, k=retrieval_settings.rrf_k)
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

        # Transform results (keep for backward compatibility)
        chunks = []
        for hit in raw_hits:
            source = hit.get("_source", {})
            chunks.append(
                {
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
            )

        # Preserve ordinary hybrid/RRF results. Exact narrowing is only for
        # identifier-like queries with an actual verbatim match.
        pre_filter_document_count = len(
            {
                chunk.get("filename")
                for chunk in chunks
                if isinstance(chunk.get("filename"), str)
            }
        )
        raw_aggregations = results.get("aggregations", {})
        document_names_aggregation = raw_aggregations.get("document_names", {})
        public_aggregations = {
            name: value
            for name, value in raw_aggregations.items()
            if name != "document_names"
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
            document_total_capped = bool(
                document_names_aggregation.get("sum_other_doc_count", 0)
            )
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
        return response

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

        source_fields = [
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
        raw_hits = response.get("hits", {}).get("hits", [])
        total_value = response.get("hits", {}).get("total", 0)
        if isinstance(total_value, dict) and total_value.get("relation", "eq") != "eq":
            raise RuntimeError("Exhaustive retrieval requires an exact snapshot chunk count")
        total_chunks = int(
            total_value.get("value", 0) if isinstance(total_value, dict) else total_value or 0
        )
        snapshot_buckets = response.get("aggregations", {}).get("snapshots", {}).get("buckets", [])
        snapshots = [
            str(bucket.get("key"))
            for bucket in snapshot_buckets
            if isinstance(bucket, dict) and bucket.get("key")
        ]
        if not snapshot_sha256:
            if not snapshots:
                return {
                    "results": [],
                    "error": (
                        "The document has no verifiable ingestion profile; "
                        "reindex it before exhaustive retrieval"
                    ),
                    "coverage": {
                        "mode": "exhaustive",
                        "document_id": resolved_document_id,
                        "complete": False,
                        "covered_chunks": 0,
                        "total_chunks": total_chunks,
                    },
                }
            if len(snapshots) != 1:
                return {
                    "results": [],
                    "error": (
                        "The document changed during exhaustive retrieval; "
                        "restart after ingestion completes"
                    ),
                    "coverage": {
                        "mode": "exhaustive",
                        "document_id": resolved_document_id,
                        "complete": False,
                        "covered_chunks": 0,
                        "total_chunks": total_chunks,
                    },
                }
            snapshot_sha256 = snapshots[0]

        chunks: list[dict[str, Any]] = []
        document_filename: str | None = None
        for hit in raw_hits:
            source = hit.get("_source", {})
            if source.get("document_content_sha256") != snapshot_sha256:
                raise RuntimeError("Exhaustive retrieval mixed document snapshots")
            if source.get("document_order_verified") is not True:
                raise RuntimeError("Exhaustive retrieval encountered an unverified document order")
            chunk_digest = source.get("chunk_content_sha256")
            text = source.get("text")
            if (
                not source.get("chunk_id")
                or not isinstance(chunk_digest, str)
                or len(chunk_digest) != 64
                or not isinstance(text, str)
            ):
                raise RuntimeError("Exhaustive retrieval encountered an unverifiable source chunk")
            recalculated_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(recalculated_digest, chunk_digest):
                raise RuntimeError("Exhaustive retrieval detected a chunk text digest mismatch")
            expected_chunk_index = covered_before + len(chunks)
            if source.get("chunk_index") != expected_chunk_index:
                raise RuntimeError("Exhaustive retrieval encountered a non-contiguous source order")
            filename = source.get("filename")
            if isinstance(filename, str) and filename.strip():
                if document_filename is None:
                    document_filename = filename.strip()
                elif document_filename != filename.strip():
                    raise RuntimeError("Exhaustive retrieval mixed document filenames")
            chunks.append(
                {
                    **source,
                    "id": hit.get("_id"),
                    "score": None,
                    "evidence_order": covered_before + len(chunks) + 1,
                }
            )

        covered_chunks = covered_before + len(chunks)
        if covered_chunks > total_chunks:
            raise RuntimeError("Exhaustive retrieval coverage exceeded snapshot size")
        complete = covered_chunks == total_chunks
        next_cursor: str | None = None
        if not complete:
            if not raw_hits or not raw_hits[-1].get("sort"):
                raise RuntimeError("Exhaustive retrieval stopped before complete coverage")
            next_cursor = encode_exhaustive_cursor(
                document_id=resolved_document_id,
                snapshot_sha256=snapshot_sha256,
                search_after=raw_hits[-1]["sort"],
                covered_chunks=covered_chunks,
                scope_sha256=scope_sha256,
            )
        coverage = {
            "mode": "exhaustive",
            "document_id": resolved_document_id,
            "filename": document_filename,
            "snapshot_sha256": snapshot_sha256,
            "covered_chunks": covered_chunks,
            "total_chunks": total_chunks,
            "coverage_ratio": 1.0 if total_chunks == 0 else covered_chunks / total_chunks,
            "complete": complete,
            "next_cursor": next_cursor,
        }
        return {"results": chunks, "total": len(chunks), "coverage": coverage}

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
    ) -> dict[str, Any]:
        """Public search method for API endpoints

        Args:
            embedding_model: Embedding model to use for search (defaults to the
                currently configured embedding model)
        """
        if evidence_mode not in {"focused", "exhaustive"}:
            raise ValueError("evidence_mode must be 'focused' or 'exhaustive'")

        # Set auth context if provided (for direct API calls)
        from config.settings import is_no_auth_mode

        if user_id and (jwt_token or is_no_auth_mode()):
            from auth_context import set_auth_context

            set_auth_context(user_id, jwt_token)

        # Set filters and limit in context if provided
        if filters:
            from auth_context import set_search_filters

            set_search_filters(filters)

        if evidence_mode == "exhaustive":
            if not user_id:
                return {"results": [], "error": "Authentication required"}
            return await self.read_document_chunks(
                document_id or "",
                user_id=user_id,
                jwt_token=jwt_token,
                filters=filters,
                cursor=cursor,
                batch_size=batch_size,
            )

        from auth_context import set_score_threshold, set_search_limit

        set_search_limit(limit)
        set_score_threshold(score_threshold)

        return await self.search_tool(
            query,
            embedding_model=embedding_model,
            group_by_document=group_by_document,
            page=page,
            page_size=page_size,
        )
