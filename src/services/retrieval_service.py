"""Deterministic retrieval v2 primitives.

The OpenSearch client and authentication remain in :mod:`search_service`.
Keeping fusion, diversity and optional reranking here makes the retrieval
policy independently testable and prevents a Langflow component export from
becoming the source of truth for API/SDK search.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetrievalSettings:
    """Validated subset of ``KnowledgeConfig`` used at query time."""

    strategy: str = "rrf"
    mode: str = "hybrid"
    lexical_candidates: int = 50
    vector_candidates: int = 50
    rrf_k: int = 60
    max_chunks_per_document: int = 3
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


def limit_chunks_per_document(
    hits: Iterable[dict[str, Any]], *, max_chunks_per_document: int
) -> list[dict[str, Any]]:
    """Keep rank order while preventing one document from monopolising context."""
    max_chunks = max(1, int(max_chunks_per_document))
    counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
        document_key = str(
            source.get("document_id") or source.get("connector_file_id") or source.get("filename") or hit_identity(hit)
        )
        if counts.get(document_key, 0) >= max_chunks:
            continue
        counts[document_key] = counts.get(document_key, 0) + 1
        selected.append(hit)
    return selected


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
            if not isinstance(index, int) or index < 0 or index >= len(hits) or index in used_indexes:
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
