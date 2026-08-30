"""Deterministic retrieval v2 primitives.

The OpenSearch client and authentication remain in :mod:`search_service`.
Keeping fusion, diversity and optional reranking here makes the retrieval
policy independently testable and prevents a Langflow component export from
becoming the source of truth for API/SDK search.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from utils.logging_config import get_logger

logger = get_logger(__name__)

EXHAUSTIVE_PROFILE_VERSION = 1
EXHAUSTIVE_BATCH_MAX = 50
PROVENANCE_GRAPH_PAGE_SIZE = 500
DEFAULT_SCOPE_RELATION_ROLES = (
    "attachment_of",
    "contained_in",
    "derived_from",
    "member_of",
    "occurrence_of",
    "primary_source",
    "references",
    "reply_to",
)

SCOPE_COVERAGE_MESSAGES = {
    "complete": (
        "The accessible provenance-connected scope discovered from the ranked seeds "
        "was closed and every discovered document snapshot was read and verified."
    ),
    "incomplete_seed_discovery": "Ranked seed discovery did not complete.",
    "search_error": "Ranked seed discovery failed with a search error.",
    "no_provenance_seed": "No valid provenance-bearing seed document was discovered.",
    "seed_missing_provenance": (
        "At least one discovered seed document has missing or invalid provenance."
    ),
    "graph_limit_reached": "A provenance graph traversal limit stopped closure.",
    "graph_traversal_failed": "Provenance graph traversal failed before closure.",
    "document_limit_reached": "The document discovery limit stopped closure.",
    "document_read_incomplete": "At least one discovered document was not read completely.",
    "legacy_document": "At least one document has no verifiable ingestion profile.",
    "snapshot_changed": "At least one document snapshot changed while it was being read.",
    "cursor_invalid": "At least one document continuation cursor was invalid.",
    "access_error": "At least one discovered document could not be read in this access scope.",
    "profile_invalid": "At least one document verification profile or coverage counter is invalid.",
    "identity_ambiguous": (
        "A provenance alternate identifier resolves to more than one accessible entity."
    ),
}

SCOPE_COVERAGE_CODE_ORDER = tuple(SCOPE_COVERAGE_MESSAGES)


@dataclass(frozen=True)
class ScopeCertificationFacts:
    """Measured facts consumed by the sole scope certification decision.

    Counts and ratios describe work performed; they never certify coverage by
    themselves. Completion additionally requires valid seeds, natural graph
    closure and verified complete reads for every accessible discovered
    document.
    """

    seed_discovery_complete: bool
    seed_documents: int
    valid_provenance_seed_documents: int
    invalid_provenance_seed_documents: int
    graph_frontier_empty: bool
    graph_limit_reached: bool
    graph_stop_reason: str | None
    graph_failed: bool
    documents_discovered: int
    documents_complete: int
    covered_chunks: int
    total_chunks: int
    document_failure_codes: tuple[str, ...] = ()
    seed_failure_code: str | None = None


def certify_scope_coverage(facts: ScopeCertificationFacts) -> dict[str, Any]:
    """Return one deterministic, fail-closed scope coverage decision."""
    failures: set[str] = set()
    if facts.seed_failure_code:
        failures.add(
            facts.seed_failure_code
            if facts.seed_failure_code in SCOPE_COVERAGE_MESSAGES
            and facts.seed_failure_code != "complete"
            else "search_error"
        )
    elif not facts.seed_discovery_complete:
        failures.add("incomplete_seed_discovery")

    if facts.seed_discovery_complete:
        if facts.seed_documents <= 0 or facts.valid_provenance_seed_documents <= 0:
            failures.add("no_provenance_seed")
        elif (
            facts.invalid_provenance_seed_documents > 0
            or facts.valid_provenance_seed_documents != facts.seed_documents
        ):
            failures.add("seed_missing_provenance")

        if facts.graph_failed:
            failures.add("graph_traversal_failed")
        if facts.graph_limit_reached:
            if facts.graph_stop_reason == "max_documents":
                failures.add("document_limit_reached")
            elif facts.graph_stop_reason == "ambiguous_alternate_id":
                failures.add("identity_ambiguous")
            else:
                failures.add("graph_limit_reached")
        elif not facts.graph_frontier_empty:
            failures.add("graph_traversal_failed")

        recognized_document_failures = {
            code
            for code in facts.document_failure_codes
            if code in SCOPE_COVERAGE_MESSAGES and code != "complete"
        }
        failures.update(recognized_document_failures)
        if facts.document_failure_codes and not recognized_document_failures:
            failures.add("document_read_incomplete")
        if (
            facts.documents_complete != facts.documents_discovered
            and not facts.document_failure_codes
        ):
            failures.add("document_read_incomplete")
    if (
        min(
            facts.seed_documents,
            facts.valid_provenance_seed_documents,
            facts.invalid_provenance_seed_documents,
            facts.documents_discovered,
            facts.documents_complete,
            facts.covered_chunks,
            facts.total_chunks,
        )
        < 0
        or facts.valid_provenance_seed_documents + facts.invalid_provenance_seed_documents
        != facts.seed_documents
        or facts.documents_complete > facts.documents_discovered
        or facts.covered_chunks > facts.total_chunks
    ):
        failures.add("profile_invalid")

    ordered_failures = [code for code in SCOPE_COVERAGE_CODE_ORDER if code in failures]
    complete = not ordered_failures
    status_code = "complete" if complete else ordered_failures[0]
    return {
        "complete": complete,
        "status_code": status_code,
        "status_message": SCOPE_COVERAGE_MESSAGES[status_code],
        "failure_codes": ordered_failures,
    }


def document_content_sha256_from_chunks(chunks: Iterable[dict[str, Any]]) -> str:
    """Recompute the canonical ingestion digest from ordered verified chunks."""
    digest = hashlib.sha256()
    for expected_index, chunk in enumerate(chunks):
        chunk_id = chunk.get("chunk_id")
        chunk_digest = chunk.get("chunk_content_sha256")
        chunk_index = chunk.get("chunk_index")
        text = chunk.get("text")
        if (
            not isinstance(chunk_id, str)
            or not chunk_id
            or not isinstance(chunk_digest, str)
            or len(chunk_digest) != 64
            or chunk_index != expected_index
            or not isinstance(text, str)
        ):
            raise ValueError("Document evidence cannot reproduce the ingestion profile")
        recalculated = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(recalculated, chunk_digest):
            raise ValueError("Document evidence contains a chunk digest mismatch")
        digest.update(chunk_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(chunk_digest.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(chunk_index).encode("ascii"))
        digest.update(b"\0")
        page = chunk.get("page")
        digest.update(str(page if page is not None else "").encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class ScopeExhaustiveSettings:
    """Safety bounds for dossier-level provenance investigation.

    Reaching a bound never proves completion: callers must expose the bound as
    the reason why the scope coverage certificate is incomplete.
    """

    seed_count: int = 100
    max_depth: int = 8
    max_entities: int = 500
    max_documents: int = 250
    batch_size: int = 50

    @classmethod
    def from_knowledge(cls, knowledge: Any) -> ScopeExhaustiveSettings:
        def bounded(name: str, default: int, maximum: int) -> int:
            try:
                return min(maximum, max(1, int(getattr(knowledge, name, default))))
            except (TypeError, ValueError):
                return default

        return cls(
            seed_count=bounded("retrieval_scope_seed_count", 100, 500),
            max_depth=bounded("retrieval_scope_max_depth", 8, 64),
            max_entities=bounded("retrieval_scope_max_entities", 500, 5000),
            max_documents=bounded("retrieval_scope_max_documents", 250, 1000),
            batch_size=bounded("retrieval_scope_batch_size", 50, EXHAUSTIVE_BATCH_MAX),
        )


@dataclass(frozen=True)
class RetrievalSettings:
    """Validated subset of ``KnowledgeConfig`` used at query time."""

    strategy: str = "rrf"
    mode: str = "hybrid"
    lexical_candidates: int = 50
    vector_candidates: int = 50
    rrf_k: int = 60
    max_chunks_per_document: int = 3
    adaptive_max_chunks_per_document: int = 20
    reranker_url: str = ""
    reranker_timeout: int = 5
    debug: bool = False

    @classmethod
    def from_knowledge(cls, knowledge: Any) -> RetrievalSettings:
        def bounded(name: str, default: int, maximum: int) -> int:
            try:
                return min(maximum, max(1, int(getattr(knowledge, name, default))))
            except (TypeError, ValueError):
                return default

        strategy = str(getattr(knowledge, "retrieval_strategy", "rrf") or "rrf")
        mode = str(getattr(knowledge, "retrieval_mode", "hybrid") or "hybrid")
        return cls(
            strategy=strategy if strategy in {"weighted", "rrf"} else "rrf",
            mode=mode if mode in {"hybrid", "lexical", "vector"} else "hybrid",
            lexical_candidates=bounded("retrieval_lexical_candidates", 50, 500),
            vector_candidates=bounded("retrieval_vector_candidates", 50, 500),
            rrf_k=bounded("retrieval_rrf_k", 60, 1000),
            max_chunks_per_document=bounded("retrieval_max_chunks_per_document", 3, 100),
            adaptive_max_chunks_per_document=bounded(
                "retrieval_adaptive_max_chunks_per_document", 20, 100
            ),
            reranker_url=str(getattr(knowledge, "retrieval_reranker_url", "") or "").strip(),
            reranker_timeout=bounded("retrieval_reranker_timeout", 5, 120),
            debug=bool(getattr(knowledge, "retrieval_debug", False)),
        )


def hit_identity(hit: dict[str, Any]) -> str:
    """Return a stable identity for a raw OpenSearch hit or result item."""
    source = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
    for key in ("chunk_id", "_id", "id"):
        value = hit.get(key) or source.get(key)
        if value is not None:
            return str(value)
    # This only occurs for malformed test/third-party responses.  Do not use
    # ``id(hit)`` here: it is process-local and makes equal-score ordering
    # non-deterministic.  The digest is stable for equivalent payloads while
    # still keeping distinct malformed items separate in normal use.
    canonical = json.dumps(source, sort_keys=True, default=str, separators=(",", ":"))
    return f"anonymous:{hashlib.sha256(canonical.encode()).hexdigest()}"


def reciprocal_rank_fusion(
    ranked_lists: Iterable[Iterable[dict[str, Any]]],
    *,
    k: int = 60,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fuse independently ranked lists using deterministic reciprocal ranks.

    The input hit is copied so adding the internal ``_retrieval_fusion_score``
    never mutates a response owned by an OpenSearch client or a caller.
    """
    score_by_id: dict[str, float] = {}
    hit_by_id: dict[str, dict[str, Any]] = {}
    safe_k = max(1, int(k))

    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked, start=1):
            identity = hit_identity(hit)
            score_by_id[identity] = score_by_id.get(identity, 0.0) + 1.0 / (safe_k + rank)
            hit_by_id.setdefault(identity, dict(hit))

    ordered_ids = sorted(
        hit_by_id,
        # A persistent identity is the final tie-breaker.  It intentionally
        # does not depend on OpenSearch's incidental response order or on the
        # order in which a lane was enumerated.
        key=lambda identity: (-score_by_id[identity], identity),
    )
    if limit is not None:
        ordered_ids = ordered_ids[: max(0, limit)]

    fused: list[dict[str, Any]] = []
    for identity in ordered_ids:
        item = hit_by_id[identity]
        item["_retrieval_fusion_score"] = score_by_id[identity]
        fused.append(item)
    return fused


def _document_key(hit: dict[str, Any]) -> str:
    source = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
    return str(
        source.get("document_id")
        or source.get("connector_file_id")
        or source.get("filename")
        or hit_identity(hit)
    )


def adaptive_chunk_limit(
    document_chunk_count: Any,
    *,
    base_chunks_per_document: int,
    adaptive_max_chunks_per_document: int,
) -> int:
    """Return a bounded per-query quota derived from the ingestion profile.

    The square-root curve lets a long document contribute more evidence without
    scaling context linearly with its size.  A 3-chunk invoice keeps all three
    chunks, a 100-chunk report may contribute ten, and very large documents are
    still bounded by the operator-controlled adaptive maximum.  Legacy chunks
    without a profile retain the historical base quota.
    """
    base = max(1, int(base_chunks_per_document))
    ceiling = max(base, int(adaptive_max_chunks_per_document))
    try:
        total = int(document_chunk_count)
    except (TypeError, ValueError):
        return base
    if total < 1:
        return base
    return min(total, ceiling, max(base, math.ceil(math.sqrt(total))))


def limit_chunks_per_document(
    hits: Iterable[dict[str, Any]],
    *,
    max_chunks_per_document: int,
    adaptive_max_chunks_per_document: int | None = None,
) -> list[dict[str, Any]]:
    """Select evidence using a diversity pass followed by adaptive fill.

    The first pass preserves the configured base quota for each represented
    document.  The second pass fills additional ranked chunks only when the
    ingestion profile proves that the document is larger.  This avoids both
    failure modes: a 100-page PDF being clipped to three chunks and one large
    PDF crowding every other relevant document out of the candidate set.
    """
    ranked_hits = list(hits)
    base = max(1, int(max_chunks_per_document))
    adaptive_ceiling = (
        base
        if adaptive_max_chunks_per_document is None
        else max(base, int(adaptive_max_chunks_per_document))
    )
    caps: dict[str, int] = {}
    for hit in ranked_hits:
        source = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
        key = _document_key(hit)
        caps[key] = adaptive_chunk_limit(
            source.get("document_chunk_count"),
            base_chunks_per_document=base,
            adaptive_max_chunks_per_document=adaptive_ceiling,
        )

    counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    # Diversity pass: every represented document can first contribute its base
    # evidence in fused rank order.
    for hit in ranked_hits:
        key = _document_key(hit)
        if counts.get(key, 0) >= min(base, caps[key]):
            continue
        identity = hit_identity(hit)
        counts[key] = counts.get(key, 0) + 1
        selected_ids.add(identity)
        selected.append(hit)

    # Adaptive fill: large profiled documents may contribute more evidence,
    # still in their original fused order and still below the configured cap.
    for hit in ranked_hits:
        identity = hit_identity(hit)
        if identity in selected_ids:
            continue
        key = _document_key(hit)
        if counts.get(key, 0) >= caps[key]:
            continue
        counts[key] = counts.get(key, 0) + 1
        selected.append(hit)
    return selected


def encode_exhaustive_cursor(
    *,
    document_id: str,
    snapshot_sha256: str,
    search_after: list[Any],
    covered_chunks: int,
    scope_sha256: str,
) -> str:
    """Encode an authenticated cursor bound to a snapshot and access scope.

    Coverage accounting must never trust counters supplied by a caller. The
    HMAC makes skipped positions or edited counters detectable, while the scope
    digest prevents replay with another principal or filter set.
    """
    payload = json.dumps(
        {
            "v": 1,
            "document_id": document_id,
            "snapshot_sha256": snapshot_sha256,
            "search_after": search_after,
            "covered_chunks": covered_chunks,
            "scope_sha256": scope_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    from config.settings import SESSION_SECRET

    signature = hmac.digest(SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), "sha256")
    encoded_signature = urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded}.{encoded_signature}"


def decode_exhaustive_cursor(
    cursor: str,
    *,
    document_id: str,
    scope_sha256: str,
) -> dict[str, Any]:
    """Authenticate and validate an exhaustive evidence continuation cursor."""
    if not cursor:
        return {}
    try:
        encoded, encoded_signature = cursor.split(".", 1)
        from config.settings import SESSION_SECRET

        expected_signature = hmac.digest(
            SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), "sha256"
        )
        signature_padding = "=" * (-len(encoded_signature) % 4)
        supplied_signature = urlsafe_b64decode(
            (encoded_signature + signature_padding).encode("ascii")
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("cursor signature mismatch")
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid exhaustive retrieval cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("Unsupported exhaustive retrieval cursor")
    if payload.get("document_id") != document_id:
        raise ValueError("Exhaustive retrieval cursor belongs to another document")
    if payload.get("scope_sha256") != scope_sha256:
        raise ValueError("Exhaustive retrieval cursor belongs to another access scope")
    search_after = payload.get("search_after")
    snapshot = payload.get("snapshot_sha256")
    covered = payload.get("covered_chunks")
    if (
        not isinstance(search_after, list)
        or not isinstance(snapshot, str)
        or len(snapshot) != 64
        or not isinstance(covered, int)
        or covered < 0
    ):
        raise ValueError("Invalid exhaustive retrieval cursor payload")
    return payload


def exhaustive_scope_sha256(*, user_id: str, filters: dict[str, Any] | None) -> str:
    """Fingerprint the DLS principal and caller filters used by an evidence read."""
    canonical = json.dumps(
        {"user_id": user_id, "filters": filters or {}},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provenance_record(hit: dict[str, Any]) -> dict[str, Any] | None:
    """Project one representative OpenSearch hit into a graph record."""
    source = hit.get("_source")
    if not isinstance(source, dict):
        return None
    document_id = source.get("document_id")
    provenance = source.get("source_provenance")
    entity = provenance.get("entity") if isinstance(provenance, dict) else None
    entity_id = source.get("source_entity_id")
    if not isinstance(entity_id, str) and isinstance(entity, dict):
        entity_id = entity.get("id")
    if not isinstance(document_id, str) or not document_id.strip():
        return None
    if not isinstance(entity_id, str) or not entity_id.strip():
        return None
    alternate_ids = source.get("source_entity_alternate_ids", [])
    if not isinstance(alternate_ids, list):
        alternate_ids = []
    generated_at_time = entity.get("generated_at_time") if isinstance(entity, dict) else None
    return {
        "document_id": document_id.strip(),
        "filename": source.get("filename"),
        "mimetype": source.get("mimetype"),
        "source_url": source.get("source_url"),
        "connector_file_id": source.get("connector_file_id"),
        "source_entity_id": entity_id.strip(),
        "source_entity_type": source.get("source_entity_type"),
        "source_entity_system": source.get("source_entity_system"),
        "source_entity_alternate_ids": sorted(
            {value.strip() for value in alternate_ids if isinstance(value, str) and value.strip()}
        ),
        "source_relative_path": source.get("source_relative_path"),
        "source_path_ancestors": source.get("source_path_ancestors", []),
        "generated_at_time": generated_at_time,
        "source_provenance": provenance,
    }


def _graph_query_body(
    entity_ids: list[str],
    *,
    allowed_roles: tuple[str, ...],
    reverse: bool,
    size: int,
    filter_clauses: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build one deterministic forward or role-safe reverse graph query."""
    if reverse:
        identity_query: dict[str, Any] = {
            "nested": {
                "path": "source_provenance.relations",
                "query": {
                    "bool": {
                        "filter": [
                            {"terms": {"source_provenance.relations.role": list(allowed_roles)}},
                            {
                                "bool": {
                                    "should": [
                                        {
                                            "terms": {
                                                "source_provenance.relations.target.id": entity_ids
                                            }
                                        },
                                        {
                                            "terms": {
                                                "source_provenance.relations.target.alternate_ids": (
                                                    entity_ids
                                                )
                                            }
                                        },
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        ]
                    }
                },
            }
        }
    else:
        identity_query = {
            "bool": {
                "should": [
                    {"terms": {"source_entity_id": entity_ids}},
                    {"terms": {"source_entity_alternate_ids": entity_ids}},
                ],
                "minimum_should_match": 1,
            }
        }

    return {
        "query": {
            "bool": {
                "must": [identity_query],
                # Provenance is copied onto every chunk. Selecting chunk zero
                # yields one stable representative without a costly collapse.
                "filter": [
                    {"term": {"chunk_index": 0}},
                    *filter_clauses,
                ],
            }
        },
        "_source": [
            "document_id",
            "filename",
            "mimetype",
            "source_url",
            "connector_file_id",
            "source_provenance",
            "source_entity_id",
            "source_entity_type",
            "source_entity_system",
            "source_entity_alternate_ids",
            "source_relative_path",
            "source_path_ancestors",
            # Included both for stability verification and to make the total
            # pagination key auditable in returned hits.
            "ingest_run_id",
            "chunk_id",
        ],
        "size": size,
        "track_total_hits": True,
        "sort": [
            {"source_entity_id": {"order": "asc", "missing": "_last"}},
            {"document_id": {"order": "asc", "missing": "_last"}},
            # A document/source occurrence can have several ingest generations.
            # ``ingest_run_id`` separates those generations and ``chunk_id``
            # uniquely identifies their representative chunk. All four fields
            # are mapped keywords with doc_values, unlike OpenSearch ``_id``.
            {"ingest_run_id": {"order": "asc", "missing": "_last"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
    }


async def expand_provenance_graph(
    client: Any,
    *,
    index_name: str,
    seed_entity_ids: Iterable[str],
    seed_documents: Iterable[dict[str, Any]] = (),
    allowed_roles: Iterable[str] = DEFAULT_SCOPE_RELATION_ROLES,
    max_depth: int = 8,
    max_entities: int = 500,
    max_documents: int = 250,
    filter_clauses: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Close the accessible PROV-O graph around seed entities.

    ``client`` must be the current user's DLS-scoped OpenSearch client. Both
    directions are queried at every depth. Reverse lookup deliberately uses a
    nested query so a role can never be paired with another relation's target.
    The returned traversal certificate is deterministic and explicitly marks
    every safety-limit stop as incomplete.
    """
    roles = tuple(sorted({str(role) for role in allowed_roles if str(role)}))
    scoped_filters = tuple(dict(clause) for clause in filter_clauses if isinstance(clause, dict))
    if not roles:
        raise ValueError("At least one provenance relation role is required")
    resolved_max_depth = max(1, int(max_depth))
    resolved_max_entities = max(1, int(max_entities))
    resolved_max_documents = max(1, int(max_documents))

    seed_document_values = [
        dict(document) for document in seed_documents if isinstance(document, dict)
    ]
    # ``document_id`` identifies content and can legitimately be shared by a
    # local import and an OpenArchiver attachment. Provenance closure operates
    # on source occurrences, so keep those occurrences distinct. A repeated
    # ingest of the *same* occurrence still shares this key and is detected by
    # the immutable document read below if its snapshot changed.
    documents: dict[tuple[str, str], dict[str, Any]] = {}

    def occurrence_key(record: dict[str, Any]) -> tuple[str, str]:
        return (str(record["document_id"]), str(record["source_entity_id"]))

    for document in seed_document_values:
        record = _provenance_record({"_source": document})
        if record is not None:
            documents.setdefault(occurrence_key(record), document)

    frontier = {
        value.strip() for value in seed_entity_ids if isinstance(value, str) and value.strip()
    }
    queried_ids: set[str] = set()
    primary_entities: set[str] = set()
    identifier_owner: dict[str, str] = {}
    pending_edges: set[tuple[str, str, str]] = set()
    depth = 0
    stop_reason = "frontier_empty"
    limit_reached = False

    def register_identity(record: dict[str, Any]) -> bool:
        """Register one accessible entity; reject aliases that would merge identities."""
        nonlocal stop_reason, limit_reached
        primary = record["source_entity_id"]
        identifiers = [primary, *record["source_entity_alternate_ids"]]
        for identifier in identifiers:
            owner = identifier_owner.get(identifier)
            if owner is not None and owner != primary:
                stop_reason = "ambiguous_alternate_id"
                limit_reached = True
                return False
        if primary not in primary_entities and len(primary_entities) >= resolved_max_entities:
            stop_reason = "max_entities"
            limit_reached = True
            return False
        primary_entities.add(primary)
        for identifier in identifiers:
            identifier_owner[identifier] = primary
        return True

    def add_record(record: dict[str, Any], next_frontier: set[str]) -> None:
        """Add one DLS-visible record and its unresolved relation identifiers."""
        nonlocal stop_reason, limit_reached
        if not register_identity(record):
            return
        key = occurrence_key(record)
        if key not in documents:
            if len(documents) >= resolved_max_documents:
                stop_reason = "max_documents"
                limit_reached = True
                return
            documents[key] = record

        primary = record["source_entity_id"]
        next_frontier.update([primary, *record["source_entity_alternate_ids"]])
        provenance = record.get("source_provenance")
        relations = provenance.get("relations", []) if isinstance(provenance, dict) else []
        for relation in relations if isinstance(relations, list) else []:
            if not isinstance(relation, dict) or relation.get("role") not in roles:
                continue
            target = relation.get("target")
            target_id = target.get("id") if isinstance(target, dict) else None
            if not isinstance(target_id, str) or not target_id.strip():
                continue
            normalized_target = target_id.strip()
            pending_edges.add((primary, str(relation["role"]), normalized_target))
            next_frontier.add(normalized_target)
            alternate_targets = target.get("alternate_ids", [])
            if isinstance(alternate_targets, list):
                normalized_alternates = {
                    value.strip()
                    for value in alternate_targets
                    if isinstance(value, str) and value.strip()
                }
                next_frontier.update(normalized_alternates)
                pending_edges.update(
                    (primary, str(relation["role"]), value) for value in normalized_alternates
                )

    # Seed manifests come from the same DLS-scoped ranked retrieval and may
    # already provide a complete canonical identity without another query.
    initial_frontier: set[str] = set(frontier)
    for document in seed_document_values:
        record = _provenance_record({"_source": document})
        if record is not None:
            add_record(record, initial_frontier)
    frontier = initial_frontier

    if len(documents) > resolved_max_documents:
        documents = dict(sorted(documents.items())[:resolved_max_documents])
        stop_reason = "max_documents"
        limit_reached = True

    pagination_pages = 0
    traversal_stats: dict[str, dict[str, int]] = {
        "forward": {"hits": 0, "pages": 0, "verification_pages": 0},
        "reverse": {"hits": 0, "pages": 0, "verification_pages": 0},
    }
    distinct_result_ids: set[str] = set()
    stability_observations = 0

    async def search_records(entity_ids: list[str]) -> list[dict[str, Any]]:
        """Read and verify both directions using only DLS-compatible searches.

        OpenSearch Security filter-level DLS rejects PIT creation. Each
        direction is therefore observed twice with ordinary ``_search`` plus
        ``search_after``. Exact totals are required on every page and the two
        canonical observations must match before any record is accepted.
        """
        nonlocal pagination_pages, stability_observations
        bodies = [
            _graph_query_body(
                entity_ids,
                allowed_roles=roles,
                reverse=reverse,
                size=PROVENANCE_GRAPH_PAGE_SIZE,
                filter_clauses=scoped_filters,
            )
            for reverse in (False, True)
        ]
        records: dict[tuple[str, str], dict[str, Any]] = {}

        async def observe(
            base_body: dict[str, Any],
        ) -> tuple[list[dict[str, Any]], str, int, int, set[str]]:
            nonlocal pagination_pages
            expected_total: int | None = None
            returned_hits = 0
            search_after: list[Any] | None = None
            seen_hit_ids: set[str] = set()
            seen_sort_keys: set[str] = set()
            canonical_hits: list[dict[str, Any]] = []
            observed_records: list[dict[str, Any]] = []
            pages = 0

            while expected_total is None or returned_hits < expected_total:
                page_body = {**base_body}
                if search_after is not None:
                    page_body["search_after"] = search_after
                response = await client.search(
                    index=index_name,
                    body=page_body,
                    params={"terminate_after": 0},
                )
                pages += 1
                pagination_pages += 1

                hit_container = response.get("hits", {})
                hits = hit_container.get("hits", [])
                if not isinstance(hits, list):
                    raise RuntimeError("Provenance traversal returned an invalid result page")
                total = hit_container.get("total", 0)
                if isinstance(total, dict):
                    if total.get("relation", "eq") != "eq":
                        raise RuntimeError("Provenance traversal requires exact hit totals")
                    total_value = int(total.get("value", 0))
                else:
                    total_value = int(total or 0)
                if total_value < 0:
                    raise RuntimeError("Provenance traversal returned an invalid hit total")
                if expected_total is None:
                    expected_total = total_value
                elif total_value != expected_total:
                    raise RuntimeError(
                        "Provenance traversal hit total changed during pagination"
                    )
                if len(hits) > PROVENANCE_GRAPH_PAGE_SIZE:
                    raise RuntimeError("Provenance traversal page exceeded its requested size")
                if not hits and returned_hits < expected_total:
                    raise RuntimeError("Provenance traversal pagination stopped before completion")

                for hit in hits:
                    hit_id = hit.get("_id")
                    if not isinstance(hit_id, str) or not hit_id:
                        raise RuntimeError("Provenance traversal page has no stable hit identity")
                    if hit_id in seen_hit_ids:
                        raise RuntimeError(
                            "Provenance traversal returned a duplicate paginated hit"
                        )
                    seen_hit_ids.add(hit_id)

                    sort_values = hit.get("sort")
                    if not isinstance(sort_values, list) or len(sort_values) != len(
                        base_body["sort"]
                    ):
                        raise RuntimeError(
                            "Provenance traversal returned an invalid search_after cursor"
                        )
                    sort_key = json.dumps(
                        sort_values,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if sort_key in seen_sort_keys:
                        raise RuntimeError("Provenance traversal sort key is not unique")
                    seen_sort_keys.add(sort_key)

                    source = hit.get("_source")
                    if not isinstance(source, dict):
                        raise RuntimeError("Provenance traversal hit has no source document")
                    canonical_hits.append(
                        {
                            "_id": hit_id,
                            "sort": sort_values,
                            "_source": source,
                        }
                    )
                    record = _provenance_record(hit)
                    if record is not None:
                        observed_records.append(record)

                returned_hits += len(hits)
                if returned_hits > expected_total:
                    raise RuntimeError(
                        "Provenance traversal pagination exceeded its exact hit total"
                    )
                if returned_hits == expected_total:
                    break
                if len(hits) < PROVENANCE_GRAPH_PAGE_SIZE:
                    raise RuntimeError(
                        "Provenance traversal returned an incomplete intermediate page"
                    )
                last_sort = hits[-1].get("sort")
                if not isinstance(last_sort, list):
                    raise RuntimeError("Provenance traversal page has no continuation cursor")
                search_after = last_sort

            canonical = json.dumps(
                canonical_hits,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            return observed_records, digest, expected_total or 0, pages, seen_hit_ids

        for reverse, base_body in zip((False, True), bodies, strict=True):
            direction = "reverse" if reverse else "forward"
            _first_records, first_digest, first_total, first_pages, first_ids = await observe(
                base_body
            )
            second_records, second_digest, second_total, second_pages, second_ids = await observe(
                base_body
            )
            if (
                first_digest != second_digest
                or first_total != second_total
                or first_ids != second_ids
            ):
                raise RuntimeError(
                    "Provenance traversal changed between stability observations"
                )

            stability_observations += 1
            traversal_stats[direction]["hits"] += second_total
            traversal_stats[direction]["pages"] += first_pages
            traversal_stats[direction]["verification_pages"] += second_pages
            distinct_result_ids.update(second_ids)
            for record in second_records:
                records[(record["source_entity_id"], record["document_id"])] = record

            logger.info(
                "Completed paginated provenance direction",
                direction=direction,
                frontier_entities=len(entity_ids),
                pages=first_pages,
                verification_pages=second_pages,
                hits=second_total,
                stability_verified=True,
            )
        return [records[key] for key in sorted(records)]

    async def traverse() -> None:
        nonlocal depth, frontier, limit_reached, stop_reason
        while frontier and not limit_reached:
            current = sorted(frontier - queried_ids)
            if not current:
                frontier = set()
                break

            # At the exact depth boundary, probe visibility only. Hidden relation
            # targets must not turn natural DLS closure into a false limit.
            if depth >= resolved_max_depth:
                boundary_records = await search_records(current)
                requires_expansion = any(
                    record["source_entity_id"] not in primary_entities
                    or occurrence_key(record) not in documents
                    for record in boundary_records
                )
                if requires_expansion:
                    stop_reason = "max_depth"
                    limit_reached = True
                    frontier = set(current)
                else:
                    queried_ids.update(current)
                    frontier = set()
                break

            queried_ids.update(current)
            records = await search_records(current)
            next_frontier: set[str] = set()
            for record in records:
                add_record(record, next_frontier)
                if limit_reached:
                    break
            depth += 1
            if limit_reached:
                frontier = next_frontier or set(current)
                break
            frontier = next_frontier - queried_ids

    if frontier and not limit_reached:
        await traverse()

    visible_edges: dict[tuple[str, str, str], dict[str, str]] = {}
    for source, role, target_identifier in pending_edges:
        target = identifier_owner.get(target_identifier)
        if target is None:
            continue
        edge_key = (source, role, target)
        visible_edges[edge_key] = {
            "source_entity_id": source,
            "role": role,
            "target_entity_id": target,
        }

    remaining_frontier = sorted(frontier)
    return {
        "documents": [documents[key] for key in sorted(documents)],
        "entities": sorted(identifier_owner),
        "edges": [visible_edges[key] for key in sorted(visible_edges)],
        "coverage": {
            "entities_visited": len(primary_entities),
            "documents_discovered": len(documents),
            "depth_reached": depth,
            "frontier_empty": not remaining_frontier,
            "limit_reached": limit_reached,
            "stop_reason": stop_reason,
            "remaining_frontier": remaining_frontier,
            "pagination_pages": pagination_pages,
            "forward_hits": traversal_stats["forward"]["hits"],
            "reverse_hits": traversal_stats["reverse"]["hits"],
            "forward_pages": traversal_stats["forward"]["pages"],
            "reverse_pages": traversal_stats["reverse"]["pages"],
            "forward_verification_pages": traversal_stats["forward"][
                "verification_pages"
            ],
            "reverse_verification_pages": traversal_stats["reverse"][
                "verification_pages"
            ],
            "distinct_results": len(distinct_result_ids),
            "stability_verified": stability_observations > 0,
            "stability_observations": stability_observations,
        },
    }


class HttpReranker:
    """Optional reranker using the common ``{query, documents}`` JSON shape.

    A failed optional reranker is deliberately non-fatal: it logs the failure
    and leaves the deterministic retrieval order untouched.
    """

    def __init__(self, url: str, timeout_seconds: int):
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def rerank(self, query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.url or not hits:
            return hits
        documents = [
            (hit.get("_source") if isinstance(hit.get("_source"), dict) else hit).get("text", "")
            for hit in hits
        ]
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.url,
                    json={"query": query, "documents": documents},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # optional integration; do not break search
            logger.warning("Retrieval reranker unavailable; using fused order", error=str(exc))
            return hits

        results = payload.get("results", payload.get("data", payload.get("scores", [])))
        if not isinstance(results, list):
            logger.warning("Retrieval reranker returned an unsupported payload")
            return hits

        scored: list[tuple[float, int, dict[str, Any]]] = []
        used_indexes: set[int] = set()
        for result in results:
            if not isinstance(result, dict):
                continue
            index = result.get("index", result.get("document_index"))
            if (
                not isinstance(index, int)
                or index < 0
                or index >= len(hits)
                or index in used_indexes
            ):
                continue
            raw_score = result.get("relevance_score", result.get("score", 0.0))
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                score = 0.0
            item = dict(hits[index])
            item["_retrieval_rerank_score"] = score
            scored.append((score, index, item))
            used_indexes.add(index)

        if not scored:
            logger.warning("Retrieval reranker returned no usable rankings")
            return hits
        scored.sort(key=lambda value: (-value[0], value[1]))
        # Preserve any documents omitted by a partial reranker response.
        return [item for _score, _index, item in scored] + [
            hit for index, hit in enumerate(hits) if index not in used_indexes
        ]
