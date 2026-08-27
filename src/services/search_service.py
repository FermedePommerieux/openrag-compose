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
from services.audit_reasoning_service import AuditReasoningService
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
# Archive audit discovery must never turn a transport batch size into a silent
# recall limit. Lexical hits are therefore read from one OpenSearch scroll
# snapshot until the result set is exhausted. Scroll is required here because
# the OpenSearch Security plugin rejects PIT creation under document-level
# security. Vector search is a ranked nearest-neighbour operation and cannot
# prove semantic completeness; its depth is expanded adaptively and any engine
# ceiling is disclosed.
ARCHIVE_AUDIT_PAGE_SIZE = 500
ARCHIVE_AUDIT_VECTOR_INITIAL_DEPTH = 100
ARCHIVE_AUDIT_VECTOR_MAX_DEPTH = 10_000
ARCHIVE_AUDIT_VECTOR_STABILITY_ROUNDS = 2
ARCHIVE_AUDIT_EXPANSION_CONCURRENCY = 4
ARCHIVE_AUDIT_DOCUMENT_READ_CONCURRENCY = 4
# Audit discovery is fed a topical query, not a bag of independent trigger
# words. Requiring all terms for one- and two-token queries, then half for
# longer queries, prevents generic request words from matching most of an
# archive while preserving multi-concept evidence. Vector lanes cover wording
# variation independently. This is an adaptive relevance rule, not a result cap.
ARCHIVE_AUDIT_LEXICAL_MINIMUM_SHOULD_MATCH = "2<50%"
ARCHIVE_AUDIT_PROVENANCE_ROLES = frozenset({"attachment_of", "member_of", "references", "reply_to"})
ARCHIVE_AUDIT_TRANSIENT_FIELDS = (
    "retrieval_relation_paths",
    "retrieval_relevance_decision",
    "retrieval_relevance_reason",
    "retrieval_supporting_document_ids",
)


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


def _calibrate_audit_vector_lanes(
    retrieval_results: dict[str, dict[str, Any]],
    audit_lane_metadata: dict[str, dict[str, Any]],
) -> None:
    """Select grounded semantic audit candidates without a fixed top-k.

    A deep k-NN query eventually returns weak neighbours for nearly every
    document. Each model's score scale is instead calibrated against documents
    independently supported by the exhausted lexical lane. An uncalibrated
    vector lane is excluded rather than turning an unknown scale into thousands
    of purportedly relevant documents; metadata makes that loss of semantic
    evidence explicit and semantic completeness remains false.
    """
    lexical_document_ids = {
        str(hit.get("_source", {}).get("document_id"))
        for lane, result in retrieval_results.items()
        if _is_audit_lexical_lane(lane)
        for hit in result.get("hits", {}).get("hits", [])
        if hit.get("_source", {}).get("document_id")
    }
    for lane, lane_result in retrieval_results.items():
        if not lane.startswith("vector:"):
            continue
        vector_hits = lane_result.get("hits", {}).get("hits", [])
        best_lexical_score_by_document: dict[str, float] = {}
        for hit in vector_hits:
            document_id = str(hit.get("_source", {}).get("document_id") or "")
            score = hit.get("_score")
            if document_id not in lexical_document_ids or not isinstance(score, (int, float)):
                continue
            previous = best_lexical_score_by_document.get(document_id)
            if previous is None or float(score) > previous:
                best_lexical_score_by_document[document_id] = float(score)

        calibration_scores = sorted(best_lexical_score_by_document.values())
        if not calibration_scores:
            lane_result["hits"]["hits"] = []
            audit_lane_metadata[lane]["selection"] = {
                "rule": "uncalibrated_excluded",
                "reason": "no_lexical_supported_document_in_vector_lane",
                "calibration_documents": 0,
                "raw_candidates": len(vector_hits),
                "selected_candidates": 0,
            }
            continue

        middle = len(calibration_scores) // 2
        threshold = (
            calibration_scores[middle]
            if len(calibration_scores) % 2
            else (calibration_scores[middle - 1] + calibration_scores[middle]) / 2
        )
        selected_hits = [
            hit
            for hit in vector_hits
            if isinstance(hit.get("_score"), (int, float)) and float(hit["_score"]) >= threshold
        ]
        lane_result["hits"]["hits"] = selected_hits
        audit_lane_metadata[lane]["selection"] = {
            "rule": "lexical_supported_median_similarity",
            "score_threshold": threshold,
            "calibration_documents": len(calibration_scores),
            "raw_candidates": len(vector_hits),
            "selected_candidates": len(selected_hits),
        }


def _hits_by_document(hits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep the first ranked hit for every stable document identity."""
    documents: dict[str, dict[str, Any]] = {}
    for hit in hits:
        document_id = str(hit.get("_source", {}).get("document_id") or "")
        if document_id:
            documents.setdefault(document_id, hit)
    return documents


def _is_audit_lexical_lane(lane: str) -> bool:
    """Return whether a lane is an exhaustively consumed lexical predicate."""
    return lane == "lexical" or lane.startswith(("lexical_expansion:", "entity_expansion:"))


def _provenance_relations(hit: dict[str, Any]) -> list[tuple[str, str]]:
    """Return role/target pairs from the canonical nested provenance object."""
    provenance = hit.get("_source", {}).get("source_provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("relations"), list):
        return []
    pairs: list[tuple[str, str]] = []
    for relation in provenance["relations"]:
        if not isinstance(relation, dict):
            continue
        role = str(relation.get("role") or "")
        target = relation.get("target")
        target_id = str(target.get("id") or "") if isinstance(target, dict) else ""
        if role in ARCHIVE_AUDIT_PROVENANCE_ROLES and target_id:
            pairs.append((role, target_id))
    return pairs


def _provenance_identity_sets(hit: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return entity identities and high-signal relation targets for one hit."""
    source = hit.get("_source", {})
    entity_ids = {
        str(value)
        for value in [
            source.get("source_entity_id"),
            *(source.get("source_entity_alternate_ids") or []),
        ]
        if value
    }
    relation_targets = {target_id for _role, target_id in _provenance_relations(hit)}
    return entity_ids, relation_targets


def _provenance_relation_paths(
    frontier: dict[str, dict[str, Any]],
    candidate: dict[str, Any],
) -> list[dict[str, str]]:
    """Explain graph expansion with observable entity identifiers and anchors."""
    candidate_entities, _candidate_targets = _provenance_identity_sets(candidate)
    candidate_source = candidate.get("_source", {})
    candidate_id = str(candidate_source.get("document_id") or "")
    paths: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for anchor_id, anchor in frontier.items():
        anchor_entities, _anchor_targets = _provenance_identity_sets(anchor)
        anchor_relations = _provenance_relations(anchor)
        candidate_relations = _provenance_relations(candidate)
        connections: list[tuple[str, str, str, str]] = []
        connections.extend(
            (role, "anchor_to_candidate", target_id, "")
            for role, target_id in anchor_relations
            if target_id in candidate_entities
        )
        connections.extend(
            (role, "candidate_to_anchor", target_id, "")
            for role, target_id in candidate_relations
            if target_id in anchor_entities
        )
        connections.extend(
            (candidate_role, "shared_target", target_id, anchor_role)
            for anchor_role, target_id in anchor_relations
            for candidate_role, candidate_target_id in candidate_relations
            if target_id == candidate_target_id
        )
        for relation_role, direction, identifier, anchor_role in connections:
            identity = (anchor_id, f"{direction}:{relation_role}:{anchor_role}", identifier)
            if identity in seen:
                continue
            seen.add(identity)
            anchor_source = anchor.get("_source", {})
            path = {
                "from_document_id": anchor_id,
                "from_filename": str(anchor_source.get("filename") or ""),
                "to_document_id": candidate_id,
                "relation_role": relation_role,
                "direction": direction,
                "via_entity_id": identifier,
                "anchor_excerpt": str(anchor_source.get("text") or "")[:1200],
            }
            if anchor_role:
                path["anchor_relation_role"] = anchor_role
            paths.append(path)
    return paths


def _propagate_provenance_paths(
    retrieval_results: dict[str, dict[str, Any]],
    provenance_hits: list[dict[str, Any]],
) -> None:
    """Attach a graph proof even when RRF keeps another lane's chunk copy."""
    paths_by_document = {
        str(hit.get("_source", {}).get("document_id")): hit.get("_source", {}).get(
            "retrieval_relation_paths", []
        )
        for hit in provenance_hits
        if hit.get("_source", {}).get("document_id")
    }
    for result in retrieval_results.values():
        for hit in result.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            paths = paths_by_document.get(str(source.get("document_id") or ""))
            if paths:
                source["retrieval_relation_paths"] = paths


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
    def __init__(
        self,
        session_manager=None,
        models_service=None,
        audit_reasoning_service: AuditReasoningService | None = None,
    ):
        self.session_manager = session_manager
        self.models_service = models_service
        self.audit_reasoning_service = audit_reasoning_service
        self._configure_provider_env()

    def _resolve_audit_reasoner(
        self,
        openrag_config: Any,
    ) -> tuple[AuditReasoningService | None, str | None]:
        """Resolve the configured audit model without making retrieval fragile."""
        if self.audit_reasoning_service is not None:
            return self.audit_reasoning_service, None
        agent_config = getattr(openrag_config, "agent", None)
        reasoning_model = str(getattr(agent_config, "llm_model", "") or "").strip()
        if not reasoning_model:
            return None, "model_not_configured"
        try:
            return AuditReasoningService(clients.patched_llm_client, reasoning_model), None
        except Exception as error:
            logger.warning(
                "Archive audit reasoning client is unavailable",
                model=reasoning_model,
                error=str(error),
            )
            return None, str(error)

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
        audit_discovery: bool = False,
    ) -> dict[str, Any]:
        """
        Use this tool to search for documents relevant to the query.

        Args:
            query (str): query string to search the corpus
            embedding_model (str): Optional override for embedding model.
                                  If not provided, uses the current embedding
                                  model from configuration.
            audit_discovery: Gather a deep, document-diverse candidate union
                for a trusted explicit exhaustive request.

        Returns:
            dict (str, Any): {"results": [chunks]} on success
        """
        from utils.embedding_fields import get_embedding_field_name

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
        lexical_candidate_depth = (
            ARCHIVE_AUDIT_PAGE_SIZE if audit_discovery else retrieval_settings.lexical_candidates
        )
        vector_candidate_depth = (
            max(retrieval_settings.vector_candidates, ARCHIVE_AUDIT_VECTOR_INITIAL_DEPTH)
            if audit_discovery
            else retrieval_settings.vector_candidates
        )
        result_limit = limit
        # Detect wildcard request ("*") to return global facets/stats without semantic search
        is_wildcard_match_all = isinstance(query, str) and query.strip() == "*"

        # Get available embedding models from corpus
        query_embeddings = {}
        available_models = []
        available_model_counts: dict[str, int] = {}
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
                available_model_counts = {
                    str(bucket["key"]): int(bucket.get("doc_count", 0))
                    for bucket in buckets
                    if bucket.get("key")
                }

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
            knn_queries: list[tuple[str, dict[str, Any]]] = []
            embedding_fields_to_check = []

            for model_name, embedding_vector in query_embeddings.items():
                field_name = get_embedding_field_name(model_name)
                embedding_fields_to_check.append(field_name)
                model_vector_depth = vector_candidate_depth
                if audit_discovery and available_model_counts.get(model_name):
                    model_vector_depth = min(
                        model_vector_depth,
                        available_model_counts[model_name],
                    )
                knn_queries.append(
                    (
                        model_name,
                        {
                            "knn": {
                                field_name: {
                                    "vector": embedding_vector,
                                    "k": model_vector_depth,
                                    "num_candidates": max(1000, model_vector_depth),
                                }
                            }
                        },
                    )
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
                            "queries": [knn_query for _model, knn_query in knn_queries],
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
            "size": result_limit,
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

        # Add score threshold only for hybrid (not meaningful for match_all)
        if not is_wildcard_match_all and score_threshold > 0:
            search_body["min_score"] = score_threshold

        # In RRF mode lexical and vector candidates are intentionally fetched
        # by separate OpenSearch requests.  Their score scales are unrelated;
        # only their ranks are fused below.  Weighted remains available as an
        # explicit compatibility strategy, while RRF is the Standard default.
        retrieval_bodies: list[tuple[str, dict[str, Any]]] = []
        if use_retrieval_v2 and not is_wildcard_match_all:
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
                "size": lexical_candidate_depth,
                "track_total_hits": audit_discovery,
                "sort": [
                    {"_score": {"order": "desc"}},
                    {"chunk_id": {"order": "asc", "missing": "_last"}},
                ],
            }
            if audit_discovery:
                multi_match = lexical_body["query"]["bool"]["should"][0]["multi_match"]
                multi_match["minimum_should_match"] = ARCHIVE_AUDIT_LEXICAL_MINIMUM_SHOULD_MATCH
            if score_threshold > 0:
                lexical_body["min_score"] = score_threshold
            if retrieval_settings.mode in {"hybrid", "lexical"}:
                retrieval_bodies.append(("lexical", lexical_body))
            if retrieval_settings.mode in {"hybrid", "vector"} and knn_queries:
                # Scores from distinct embedding spaces are not calibrated.
                # Preserve one ranked lane per model and let RRF converge ranks
                # instead of comparing them inside a ``dis_max`` query.
                for model_name, knn_query in knn_queries:
                    vector_body: dict[str, Any] = {
                        "query": {
                            "bool": {
                                "must": [knn_query],
                                "filter": all_filters,
                            }
                        },
                        "aggs": _build_file_facet_aggregations(),
                        "_source": source_fields,
                        "size": next(iter(knn_query["knn"].values()))["k"],
                        "track_total_hits": audit_discovery,
                        "sort": [
                            {"_score": {"order": "desc"}},
                            {"chunk_id": {"order": "asc", "missing": "_last"}},
                        ],
                    }
                    if score_threshold > 0:
                        vector_body["min_score"] = score_threshold
                    retrieval_bodies.append((f"vector:{model_name}", vector_body))
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
        audit_reasoner: AuditReasoningService | None = None
        audit_reasoner_error: str | None = None
        if audit_discovery:
            audit_reasoner, audit_reasoner_error = self._resolve_audit_reasoner(openrag_config)

        from opensearchpy.exceptions import RequestError

        from utils.opensearch_utils import (
            DISK_SPACE_ERROR_MESSAGE,
            OpenSearchDiskSpaceError,
            is_disk_space_error,
        )

        search_params = {"terminate_after": 0}

        async def execute_search(
            body: dict[str, Any],
            label: str,
            *,
            extra_params: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            fallback_body = without_num_candidates(body)
            request_params = {**search_params, **(extra_params or {})}

            async def run(search_body: dict[str, Any]) -> dict[str, Any]:
                kwargs: dict[str, Any] = {
                    "body": search_body,
                    "params": request_params,
                }
                kwargs["index"] = get_index_name()
                return await opensearch_client.search(**kwargs)

            try:
                index_name = get_index_name()
                logger.info("Sending query to index", retrieval_lane=label, index_name=index_name)
                return await run(body)
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
                        return await run(fallback_body)
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

        async def execute_scroll_audit(
            body: dict[str, Any], label: str
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            """Read every match from one DLS-compatible scroll snapshot."""
            hits: list[dict[str, Any]] = []
            first_result: dict[str, Any] | None = None
            pages = 0
            exhausted = False
            scroll_id: str | None = None
            try:
                while True:
                    if first_result is None:
                        page_body = copy.deepcopy(body)
                        page_body["size"] = ARCHIVE_AUDIT_PAGE_SIZE
                        page_body["track_total_hits"] = True
                        page_result = await execute_search(
                            page_body,
                            label,
                            extra_params={"scroll": "5m"},
                        )
                    else:
                        if not scroll_id:
                            raise RuntimeError(
                                f"OpenSearch {label} audit omitted its scroll identifier"
                            )
                        page_result = await opensearch_client.scroll(
                            body={"scroll_id": scroll_id, "scroll": "5m"}
                        )
                    pages += 1
                    if first_result is None:
                        first_result = page_result
                    next_scroll_id = page_result.get("_scroll_id")
                    if next_scroll_id:
                        scroll_id = str(next_scroll_id)
                    page_hits = page_result.get("hits", {}).get("hits", [])
                    hits.extend(page_hits)
                    total = page_result.get("hits", {}).get("total", {})
                    total_value = total.get("value") if isinstance(total, dict) else total
                    if not page_hits or (isinstance(total_value, int) and len(hits) >= total_value):
                        exhausted = True
                        break
            finally:
                if scroll_id:
                    await opensearch_client.clear_scroll(body={"scroll_id": [scroll_id]})

            merged = dict(first_result or {})
            merged["hits"] = {
                **(merged.get("hits", {}) if isinstance(merged.get("hits"), dict) else {}),
                "hits": hits,
            }
            return merged, {
                "pages": pages,
                "returned": len(hits),
                "matching": merged.get("hits", {}).get("total"),
                "exhausted": exhausted,
                "snapshot": "scroll",
                "truncated": not exhausted,
            }

        async def execute_lexical_audit(
            body: dict[str, Any], label: str
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            """Read every lexical match and disclose its exact predicate."""
            result, metadata = await execute_scroll_audit(body, label)
            metadata.update(
                {
                    "query_rule": {
                        "type": "adaptive_minimum_should_match",
                        "minimum_should_match": ARCHIVE_AUDIT_LEXICAL_MINIMUM_SHOULD_MATCH,
                    },
                }
            )
            return result, metadata

        def _set_vector_depth(body: dict[str, Any], depth: int) -> None:
            """Update the single k-NN clause in one model-specific lane."""
            bool_query = body.get("query", {}).get("bool", {})
            for clause in bool_query.get("must", []):
                for spec in clause.get("knn", {}).values():
                    spec["k"] = depth
                    spec["num_candidates"] = max(1000, depth)
                    body["size"] = depth

        async def execute_vector_audit(
            body: dict[str, Any], label: str
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            """Deepen one semantic lane and disclose convergence or truncation."""
            model_name = label.removeprefix("vector:")
            corpus_depth = available_model_counts.get(model_name, ARCHIVE_AUDIT_VECTOR_MAX_DEPTH)
            maximum_depth = min(max(1, corpus_depth), ARCHIVE_AUDIT_VECTOR_MAX_DEPTH)
            depth = min(maximum_depth, max(1, body.get("size", 1)))
            stable_rounds = 0
            previous_documents: set[str] = set()
            merged_by_id: dict[str, dict[str, Any]] = {}
            deepest_result: dict[str, Any] = {}
            attempts: list[dict[str, Any]] = []

            while True:
                request_body = copy.deepcopy(body)
                _set_vector_depth(request_body, depth)
                deepest_result = await execute_search(request_body, label)
                lane_hits = deepest_result.get("hits", {}).get("hits", [])
                current_documents = {
                    str(hit.get("_source", {}).get("document_id"))
                    for hit in lane_hits
                    if hit.get("_source", {}).get("document_id")
                }
                new_documents = current_documents - previous_documents
                stable_rounds = stable_rounds + 1 if not new_documents else 0
                previous_documents.update(current_documents)
                for hit in lane_hits:
                    identity = str(hit.get("_source", {}).get("chunk_id") or hit.get("_id"))
                    merged_by_id.setdefault(identity, hit)
                attempts.append(
                    {
                        "depth": depth,
                        "returned": len(lane_hits),
                        "new_documents": len(new_documents),
                    }
                )

                engine_exhausted = len(lane_hits) < depth or depth >= corpus_depth
                converged = stable_rounds >= ARCHIVE_AUDIT_VECTOR_STABILITY_ROUNDS
                if engine_exhausted or converged or depth >= maximum_depth:
                    break
                depth = min(maximum_depth, depth * 2)

            deepest_hits = deepest_result.get("hits", {}).get("hits", [])
            ranked_ids = {
                str(hit.get("_source", {}).get("chunk_id") or hit.get("_id"))
                for hit in deepest_hits
            }
            merged_hits = [*deepest_hits]
            merged_hits.extend(hit for key, hit in merged_by_id.items() if key not in ranked_ids)
            merged = dict(deepest_result)
            merged["hits"] = {
                **(merged.get("hits", {}) if isinstance(merged.get("hits"), dict) else {}),
                "hits": merged_hits,
            }
            engine_exhausted = len(deepest_hits) < depth or depth >= corpus_depth
            converged = stable_rounds >= ARCHIVE_AUDIT_VECTOR_STABILITY_ROUNDS
            return merged, {
                "attempts": attempts,
                "returned": len(merged_hits),
                "documents_found": len(previous_documents),
                "depth_reached": depth,
                "corpus_vectors": corpus_depth,
                "engine_exhausted": engine_exhausted,
                "converged": converged,
                "semantic_completeness_certified": False,
                "truncated": not engine_exhausted and not converged,
            }

        async def execute_provenance_audit(
            seed_hits: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            """Traverse the visible high-signal PROV-O graph to a fixpoint.

            ``contained_in`` is intentionally absent: it identifies the source
            archive and would connect unrelated mail. Thread membership,
            replies, RFC references and attachments are contextual relations
            capable of resolving phrases such as "your project".
            """
            discovered = _hits_by_document(seed_hits)
            frontier = dict(discovered)
            expanded_hits: list[dict[str, Any]] = []
            iterations: list[dict[str, Any]] = []
            while frontier:
                entity_ids: set[str] = set()
                relation_targets: set[str] = set()
                for hit in frontier.values():
                    hit_entities, hit_targets = _provenance_identity_sets(hit)
                    entity_ids.update(hit_entities)
                    relation_targets.update(hit_targets)
                if not entity_ids and not relation_targets:
                    frontier = {}
                    break

                should: list[dict[str, Any]] = []
                if relation_targets:
                    should.extend(
                        [
                            {"terms": {"source_entity_id": sorted(relation_targets)}},
                            {"terms": {"source_entity_alternate_ids": sorted(relation_targets)}},
                            {
                                "nested": {
                                    "path": "source_provenance.relations",
                                    "query": {
                                        "bool": {
                                            "filter": [
                                                {
                                                    "terms": {
                                                        "source_provenance.relations.role": sorted(
                                                            ARCHIVE_AUDIT_PROVENANCE_ROLES
                                                        )
                                                    }
                                                },
                                                {
                                                    "terms": {
                                                        "source_provenance.relations.target.id": sorted(
                                                            relation_targets
                                                        )
                                                    }
                                                },
                                            ]
                                        }
                                    },
                                }
                            },
                        ]
                    )
                if entity_ids:
                    should.append(
                        {
                            "nested": {
                                "path": "source_provenance.relations",
                                "query": {
                                    "bool": {
                                        "filter": [
                                            {
                                                "terms": {
                                                    "source_provenance.relations.role": sorted(
                                                        ARCHIVE_AUDIT_PROVENANCE_ROLES
                                                    )
                                                }
                                            },
                                            {
                                                "terms": {
                                                    "source_provenance.relations.target.id": sorted(
                                                        entity_ids
                                                    )
                                                }
                                            },
                                        ]
                                    }
                                },
                            }
                        }
                    )

                relation_body = {
                    "query": {
                        "bool": {
                            "should": should,
                            "minimum_should_match": 1,
                            "filter": filter_clauses,
                        }
                    },
                    "_source": source_fields,
                    "size": ARCHIVE_AUDIT_PAGE_SIZE,
                    "track_total_hits": True,
                    "sort": [
                        {"_score": {"order": "desc"}},
                        {"chunk_id": {"order": "asc", "missing": "_last"}},
                    ],
                }
                relation_result, page_metadata = await execute_scroll_audit(
                    relation_body,
                    f"provenance:{len(iterations) + 1}",
                )
                related = _hits_by_document(relation_result.get("hits", {}).get("hits", []))
                new_documents = {
                    document_id: hit
                    for document_id, hit in related.items()
                    if document_id not in discovered
                }
                for hit in new_documents.values():
                    source = hit.get("_source", {})
                    source["retrieval_relation_paths"] = _provenance_relation_paths(frontier, hit)
                    expanded_hits.append(hit)
                iterations.append(
                    {
                        "frontier_documents": len(frontier),
                        "entity_ids": len(entity_ids),
                        "relation_targets": len(relation_targets),
                        "matched_documents": len(related),
                        "new_documents": len(new_documents),
                        "pages": page_metadata["pages"],
                        "exhausted": page_metadata["exhausted"],
                    }
                )
                discovered.update(new_documents)
                frontier = new_documents

            return {"hits": {"hits": expanded_hits}}, {
                "seed_documents": len(_hits_by_document(seed_hits)),
                "documents_found": len(expanded_hits),
                "closure_documents": len(discovered),
                "iterations": iterations,
                "roles": sorted(ARCHIVE_AUDIT_PROVENANCE_ROLES),
                "ignored_roles": ["contained_in"],
                "fixpoint_reached": not frontier,
                "exhausted": all(item["exhausted"] for item in iterations),
                "snapshot": "scroll_per_frontier",
                "truncated": False,
            }

        retrieval_results: dict[str, dict[str, Any]] = {}
        audit_lane_metadata: dict[str, dict[str, Any]] = {}
        audit_query_expansion: dict[str, Any] = {
            "available": False,
            "reason": (
                "not_requested"
                if not audit_discovery
                else audit_reasoner_error
                if audit_reasoner_error in {"model_not_configured"}
                else "model_client_unavailable"
                if audit_reasoner_error is not None
                else "model_not_configured"
            ),
        }
        audit_contextual_review: dict[str, Any] = {
            **audit_query_expansion,
        }
        if audit_reasoner_error:
            audit_query_expansion["error"] = audit_reasoner_error
            audit_contextual_review["error"] = audit_reasoner_error
        if retrieval_bodies:
            lanes = [name for name, _body in retrieval_bodies]
            if audit_discovery:

                async def execute_audit_lane(
                    name: str, body: dict[str, Any]
                ) -> tuple[dict[str, Any], dict[str, Any]]:
                    if name == "lexical":
                        return await execute_lexical_audit(body, name)
                    return await execute_vector_audit(body, name)

                audit_results = await asyncio.gather(
                    *[execute_audit_lane(name, body) for name, body in retrieval_bodies]
                )
                lane_results = [result for result, _metadata in audit_results]
                audit_lane_metadata = {
                    name: metadata
                    for name, (_result, metadata) in zip(lanes, audit_results, strict=True)
                }
            else:
                lane_results = await asyncio.gather(
                    *[execute_search(body, name) for name, body in retrieval_bodies]
                )
            retrieval_results = dict(zip(lanes, lane_results, strict=True))
            results = retrieval_results.get("lexical") or lane_results[0]
        else:
            results = await execute_search(search_body, "weighted")

        raw_hits = results.get("hits", {}).get("hits", [])
        if retrieval_results:
            if audit_discovery and "lexical" in retrieval_results:
                if audit_reasoner is not None:
                    expansion, audit_query_expansion = await audit_reasoner.expand_query(
                        query,
                        retrieval_results["lexical"].get("hits", {}).get("hits", []),
                    )
                    expansion_jobs: list[tuple[str, dict[str, Any], str]] = []
                    for index, expanded_query in enumerate(expansion.queries, start=1):
                        expansion_body = copy.deepcopy(lexical_body)
                        expansion_bool = expansion_body["query"]["bool"]
                        expansion_bool["should"][0]["multi_match"]["query"] = expanded_query
                        expansion_bool["should"][1]["match_phrase_prefix"]["text"]["query"] = (
                            expanded_query
                        )
                        expansion_jobs.append(
                            (f"lexical_expansion:{index}", expansion_body, expanded_query)
                        )
                    for index, entity in enumerate(expansion.entities, start=1):
                        entity_body = copy.deepcopy(lexical_body)
                        entity_body["query"]["bool"]["should"] = [
                            {
                                "multi_match": {
                                    "query": entity,
                                    "fields": ["text^2", "filename^1.5"],
                                    "type": "best_fields",
                                    "operator": "and",
                                }
                            },
                            {"match_phrase": {"text": {"query": entity}}},
                        ]
                        expansion_jobs.append((f"entity_expansion:{index}", entity_body, entity))
                    if expansion_jobs:
                        expansion_semaphore = asyncio.Semaphore(ARCHIVE_AUDIT_EXPANSION_CONCURRENCY)

                        async def execute_expansion(
                            lane: str, body: dict[str, Any]
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
                            async with expansion_semaphore:
                                if lane.startswith("entity_expansion:"):
                                    result, metadata = await execute_scroll_audit(body, lane)
                                    metadata["query_rule"] = {"type": "grounded_entity_phrase"}
                                    return result, metadata
                                return await execute_lexical_audit(body, lane)

                        expansion_results = await asyncio.gather(
                            *[
                                execute_expansion(lane, body)
                                for lane, body, _query in expansion_jobs
                            ]
                        )
                        for (lane, _body, lane_query), (
                            expansion_result,
                            expansion_metadata,
                        ) in zip(expansion_jobs, expansion_results, strict=True):
                            expansion_metadata["query"] = lane_query
                            retrieval_results[lane] = expansion_result
                            audit_lane_metadata[lane] = expansion_metadata

                _calibrate_audit_vector_lanes(retrieval_results, audit_lane_metadata)
                provenance_seed_hits = [
                    hit
                    for result in retrieval_results.values()
                    for hit in result.get("hits", {}).get("hits", [])
                ]
                provenance_result, provenance_metadata = await execute_provenance_audit(
                    provenance_seed_hits
                )
                retrieval_results["provenance"] = provenance_result
                audit_lane_metadata["provenance"] = provenance_metadata
                _propagate_provenance_paths(
                    retrieval_results,
                    provenance_result.get("hits", {}).get("hits", []),
                )
            ranked_lists = [
                lane_result.get("hits", {}).get("hits", [])
                for lane_result in retrieval_results.values()
            ]
            raw_hits = reciprocal_rank_fusion(ranked_lists, k=retrieval_settings.rrf_k)
            raw_hits = limit_chunks_per_document(
                raw_hits,
                max_chunks_per_document=(
                    1 if audit_discovery else retrieval_settings.max_chunks_per_document
                ),
                adaptive_max_chunks_per_document=(
                    1 if audit_discovery else retrieval_settings.adaptive_max_chunks_per_document
                ),
            )
            if audit_discovery and audit_reasoner is not None:
                raw_hits, audit_contextual_review = await audit_reasoner.review_candidates(
                    query,
                    raw_hits,
                )
            raw_hits = await HttpReranker(
                retrieval_settings.reranker_url,
                retrieval_settings.reranker_timeout,
            ).rerank(query, raw_hits)
            if not audit_discovery:
                raw_hits = raw_hits[:result_limit]

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
                    "retrieval_relation_paths": source.get("retrieval_relation_paths", []),
                    "retrieval_relevance_decision": source.get("retrieval_relevance_decision"),
                    "retrieval_relevance_reason": source.get("retrieval_relevance_reason"),
                    "retrieval_supporting_document_ids": source.get(
                        "retrieval_supporting_document_ids", []
                    ),
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
        chunks, aggregations = _apply_exact_match_file_filter(
            query,
            chunks,
            _normalize_file_facet_aggregations(results.get("aggregations", {})),
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
        if audit_discovery:
            lexical_lanes = {
                lane: metadata
                for lane, metadata in audit_lane_metadata.items()
                if _is_audit_lexical_lane(lane)
            }
            response["discovery"] = {
                "mode": "archive_audit",
                "documents_found": len(
                    {item.get("document_id") for item in chunks if item.get("document_id")}
                ),
                "chunks_returned": len(chunks),
                "lanes": audit_lane_metadata,
                "query_expansion": audit_query_expansion,
                "contextual_review": audit_contextual_review,
                "truncated": any(
                    lane.get("truncated") is True for lane in audit_lane_metadata.values()
                ),
                "lexical_completeness_certified": (
                    bool(lexical_lanes)
                    and all(
                        metadata.get("exhausted") is True for metadata in lexical_lanes.values()
                    )
                    if lexical_lanes
                    else None
                ),
                "provenance_completeness_certified": (
                    audit_lane_metadata.get("provenance", {}).get("fixpoint_reached") is True
                    and audit_lane_metadata.get("provenance", {}).get("exhausted") is True
                ),
                "contextual_review_complete": (
                    audit_contextual_review.get("available") is True
                    and audit_contextual_review.get("failed_batches") == 0
                    and audit_contextual_review.get("missing_decisions", 0) == 0
                    and audit_contextual_review.get("invalid_decisions", 0) == 0
                ),
                "semantic_completeness_certified": False,
            }
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
            "snapshot_sha256": snapshot_sha256,
            "covered_chunks": covered_chunks,
            "total_chunks": total_chunks,
            "coverage_ratio": 1.0 if total_chunks == 0 else covered_chunks / total_chunks,
            "complete": complete,
            "next_cursor": next_cursor,
        }
        return {"results": chunks, "total": len(chunks), "coverage": coverage}

    async def _read_archive_audit_documents(
        self,
        discovery_results: list[dict[str, Any]],
        *,
        user_id: str,
        jwt_token: str | None,
        filters: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read every candidate snapshot with bounded I/O concurrency.

        Concurrency limits transport pressure only. There is deliberately no
        document or chunk limit, and each document's continuation chain remains
        sequential so its snapshot certificate cannot be reordered.
        """
        documents: list[dict[str, Any]] = []
        seen_document_ids: set[str] = set()
        for result in discovery_results:
            document_id = str(result.get("document_id") or "").strip()
            if not document_id or document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            documents.append(
                {
                    "document_id": document_id,
                    "filename": result.get("filename"),
                    **{
                        field: result[field]
                        for field in ARCHIVE_AUDIT_TRANSIENT_FIELDS
                        if field in result and result[field] not in (None, "", [], {})
                    },
                }
            )

        semaphore = asyncio.Semaphore(ARCHIVE_AUDIT_DOCUMENT_READ_CONCURRENCY)

        async def read_document(
            document: dict[str, Any],
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            async with semaphore:
                chunks: list[dict[str, Any]] = []
                cursor = ""
                seen_cursors: set[str] = set()
                final_coverage: dict[str, Any] = {}
                while True:
                    payload = await self.read_document_chunks(
                        document["document_id"],
                        user_id=user_id,
                        jwt_token=jwt_token,
                        filters=filters,
                        cursor=cursor,
                        batch_size=EXHAUSTIVE_BATCH_MAX,
                    )
                    for chunk in payload.get("results", []):
                        if not isinstance(chunk, dict):
                            continue
                        enriched = dict(chunk)
                        for field in ARCHIVE_AUDIT_TRANSIENT_FIELDS:
                            if field in document:
                                enriched[field] = document[field]
                        chunks.append(enriched)
                    coverage = payload.get("coverage")
                    if not isinstance(coverage, dict):
                        final_coverage = {
                            "mode": "exhaustive",
                            "document_id": document["document_id"],
                            "complete": False,
                            "error": payload.get("error") or "missing coverage certificate",
                        }
                        break
                    final_coverage = dict(coverage)
                    if coverage.get("complete") is True:
                        break
                    next_cursor = str(coverage.get("next_cursor") or "").strip()
                    if not next_cursor or next_cursor in seen_cursors:
                        final_coverage["complete"] = False
                        final_coverage["error"] = (
                            payload.get("error")
                            or "incomplete coverage returned no fresh continuation cursor"
                        )
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                final_coverage["filename"] = document.get("filename")
                return chunks, final_coverage

        document_reads = await asyncio.gather(*[read_document(document) for document in documents])
        chunks = [
            chunk for document_chunks, _coverage in document_reads for chunk in document_chunks
        ]
        coverages = [coverage for _chunks, coverage in document_reads]
        return chunks, coverages

    async def _orchestrate_archive_audit(
        self,
        query: str,
        discovery_result: dict[str, Any],
        *,
        user_id: str,
        jwt_token: str | None,
        filters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Turn discovery into bounded, citation-preserving hierarchical evidence."""
        discovery_hits = [
            item for item in discovery_result.get("results", []) if isinstance(item, dict)
        ]
        chunks, document_coverages = await self._read_archive_audit_documents(
            discovery_hits,
            user_id=user_id,
            jwt_token=jwt_token,
            filters=filters,
        )
        documents_complete = sum(
            coverage.get("complete") is True for coverage in document_coverages
        )
        read_complete = documents_complete == len(document_coverages)
        coverage = {
            "mode": "exhaustive",
            "requested": True,
            "scope": "archive_audit_candidates",
            "complete": read_complete,
            "documents_complete": documents_complete,
            "documents_total": len(document_coverages),
            "documents": document_coverages,
        }

        openrag_config = get_openrag_config()
        reasoner, reasoner_error = self._resolve_audit_reasoner(openrag_config)
        if not read_complete:
            synthesis: dict[str, Any] = {
                "schema_version": "1.0",
                "strategy": "hierarchical_verified_map_reduce",
                "complete": False,
                "verified": False,
                "error": "Full-document evidence coverage is incomplete.",
            }
        elif reasoner is None:
            synthesis = {
                "schema_version": "1.0",
                "strategy": "hierarchical_verified_map_reduce",
                "complete": False,
                "verified": False,
                "error": reasoner_error or "Audit reasoning model is not configured.",
            }
        else:
            synthesis, synthesis_coverage = await reasoner.synthesize_evidence(query, chunks)
            synthesis_coverage.update(
                {
                    "documents_total": len(document_coverages),
                    "documents_complete": documents_complete,
                }
            )

        discovery = dict(discovery_result.get("discovery") or {})
        discovery["hierarchical_synthesis"] = {
            "strategy": synthesis.get("strategy"),
            "complete": synthesis.get("complete") is True,
            "verified": synthesis.get("verified") is True,
            "model": synthesis.get("model"),
            "coverage": synthesis.get("coverage"),
            "error": synthesis.get("error"),
        }
        return {
            **discovery_result,
            "results": chunks,
            "total": len(chunks),
            "coverage": coverage,
            "audit_synthesis": synthesis,
            "discovery": discovery,
        }

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
    ) -> dict[str, Any]:
        """Public search method for API endpoints

        Args:
            embedding_model: Embedding model to use for search (defaults to the
                currently configured embedding model)
        """
        if evidence_mode not in {"focused", "audit", "exhaustive"}:
            raise ValueError("evidence_mode must be 'focused', 'audit' or 'exhaustive'")

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

        result = await self.search_tool(
            query,
            embedding_model=embedding_model,
            audit_discovery=evidence_mode == "audit",
        )
        if evidence_mode == "audit" and user_id and not result.get("error"):
            return await self._orchestrate_archive_audit(
                query,
                result,
                user_id=user_id,
                jwt_token=jwt_token,
                filters=filters,
            )
        return result
