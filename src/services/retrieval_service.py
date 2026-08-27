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
import re
import unicodedata
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from utils.logging_config import get_logger

logger = get_logger(__name__)

EXHAUSTIVE_PROFILE_VERSION = 1
EXHAUSTIVE_BATCH_MAX = 50

_EXHAUSTIVE_INTENT_PATTERNS = (
    re.compile(r"\bexhausti\w*\b"),
    re.compile(r"\bcouverture\s+(?:exhaustive|complete)\b"),
    re.compile(r"\brecherche\s+(?:exhaustive|complete)\b"),
    re.compile(r"\bverifi\w*\s+tout\b"),
    re.compile(r"\b(?:tous|toutes)\s+les\b"),
    re.compile(r"\btoute\s+l[' ]archive\b"),
    re.compile(r"\b(?:find|list|read|check)\s+all\b"),
    re.compile(r"\b(?:complete|full)\s+(?:coverage|archive|corpus)\b"),
    re.compile(r"\bevery\s+(?:document|email|mail|occurrence|source)\b"),
)

_AUDIT_TOPIC_MARKER = re.compile(
    r"\b(?:th[eé]matique|sujet|topic)\b\s*(?:(?:de|du|des)\s+|[:\-]\s*)?",
    re.IGNORECASE,
)
_AUDIT_TRAILING_INSTRUCTION = re.compile(
    r"(?:[.!?;]\s*|\s+-\s+)"
    r"(?:j?e\s+veux|je\s+souhaite|i\s+want|v[eé]rif\w*\s+tout)\b",
    re.IGNORECASE,
)
_AUDIT_TOPIC_CONNECTORS = frozenset(
    {
        "a",
        "au",
        "aux",
        "avec",
        "de",
        "des",
        "du",
        "en",
        "et",
        "la",
        "le",
        "les",
        "lien",
        "ou",
        "the",
        "to",
        "with",
    }
)


def exhaustive_retrieval_requested(prompt: str) -> bool:
    """Return whether the user explicitly requests exhaustive evidence work.

    This detector is deliberately limited to explicit French and English
    formulations.  It does not decide relevance and it never broadens the
    caller's ACL/filter scope; it only prevents an explicit completeness
    request from being silently downgraded to ranked focused retrieval.
    """
    normalized = unicodedata.normalize("NFKD", str(prompt or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False
    if re.search(r"\bne\b.{0,80}\bpas\b.{0,80}\bexhausti\w*\b", normalized) or re.search(
        r"\b(?:pas|non|sans)\s+(?:de\s+)?(?:recherche\s+)?exhausti\w*\b", normalized
    ):
        return False
    return any(pattern.search(normalized) for pattern in _EXHAUSTIVE_INTENT_PATTERNS)


def audit_topic_query(prompt: str) -> str:
    """Extract the topical predicate from explicit audit instructions.

    OpenSearch must rank the subject, not conversational control words such as
    ``recherche exhaustive`` or ``toute l'archive``.  Those words express the
    required execution contract but have no evidentiary value and can match a
    large fraction of email archives.  This deterministic extraction is used
    only when a trusted caller has already selected audit mode; the complete
    user request remains unchanged for evidence synthesis and final answering.

    A marker such as ``thématique``/``sujet``/``topic`` is required before any
    text is removed.  If extraction is ambiguous, the original query is
    returned fail-open.  The deliberately tolerated ``e veux`` spelling covers
    a real copied production request without weakening exhaustive-intent
    detection.
    """

    original = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not original:
        return original
    marker = _AUDIT_TOPIC_MARKER.search(original)
    if marker is None:
        return original
    topic = original[marker.end() :].strip()
    trailing = _AUDIT_TRAILING_INSTRUCTION.search(topic)
    if trailing is not None:
        topic = topic[: trailing.start()]
    topic = topic.strip(" \t\r\n\"'“”‘’.,;:!?-")
    terms = re.findall(r"[\w]+(?:[-'][\w]+)*", topic, flags=re.UNICODE)
    topical_terms = [term for term in terms if term.casefold() not in _AUDIT_TOPIC_CONNECTORS]
    if topical_terms:
        topic = " ".join(topical_terms)
    # A one-character extraction is more likely punctuation or a malformed
    # instruction than a useful archive predicate.
    return topic if len(topic) >= 2 else original


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
