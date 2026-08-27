"""Content-addressed research memory for structured AI verification calls.

Truth and authorization are part of the cache contract:

* the caller supplies a stable user/ACL scope;
* the exact digest includes the complete evidence prompt, model and JSON schema;
* source text changes therefore produce a different key automatically;
* near-equivalent wording may reuse only an otherwise identical contract;
* related prior expansions are additive discovery hints, never evidence;
* a cache failure is fail-open and never prevents a fresh provider request.

The service is intentionally limited to validated structured responses.  It
does not cache final prose answers, whose wording and conversational context
may change independently of the documentary evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from db import engine as db_engine
from db.models.ai_response_cache import AIResponseCache
from db.repositories.ai_response_cache_repo import AIResponseCacheRepo
from utils.logging_config import get_logger

logger = get_logger(__name__)

AI_RESPONSE_CACHE_VERSION = "1"
AI_RESPONSE_CACHE_NAMESPACE = "audit_structured_response"
AI_QUERY_PROFILE_VERSION = "1"
AI_QUERY_PROJECTION_DIMENSIONS = 128
AI_QUERY_EQUIVALENT_COSINE = 0.985
AI_QUERY_RELATED_COSINE = 0.88
# Documentary proofs are durable research work, not an ephemeral HTTP cache.
# Zero therefore means "no expiry"; disabling storage is a separate flag so
# an operator cannot accidentally discard reusable evidence by selecting an
# unlimited retention policy.
DEFAULT_AI_RESPONSE_CACHE_TTL_DAYS = 0

_QUERY_COMMAND_TERMS = {
    "archive",
    "archives",
    "complet",
    "complete",
    "completement",
    "exhaustif",
    "exhaustive",
    "faire",
    "fais",
    "fasse",
    "recherche",
    "rechercher",
    "tout",
    "tous",
    "toute",
    "toutes",
    "verifie",
    "verifier",
    "verifies",
    "veux",
}
_QUERY_STOP_TERMS = {
    "a",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "cette",
    "dans",
    "de",
    "des",
    "du",
    "en",
    "et",
    "je",
    "la",
    "le",
    "les",
    "lien",
    "ou",
    "par",
    "pour",
    "que",
    "qui",
    "sur",
    "the",
    "to",
    "tu",
    "un",
    "une",
}
_QUERY_LOGIC_TERMS = {
    "apres",
    "avant",
    "entre",
    "except",
    "exclure",
    "inclure",
    "moins",
    "non",
    "pas",
    "plus",
    "sans",
    "sauf",
    "seulement",
    "uniquement",
}


def _cache_enabled() -> bool:
    raw = os.getenv("OPENRAG_AI_RESPONSE_CACHE_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _ttl_days() -> int:
    raw = os.getenv("OPENRAG_AI_RESPONSE_CACHE_TTL_DAYS", "").strip()
    if not raw:
        return DEFAULT_AI_RESPONSE_CACHE_TTL_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid OPENRAG_AI_RESPONSE_CACHE_TTL_DAYS; using default",
            value=raw,
            default=DEFAULT_AI_RESPONSE_CACHE_TTL_DAYS,
        )
        return DEFAULT_AI_RESPONSE_CACHE_TTL_DAYS


def _profile_tokens(query: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", str(query or ""))
    ascii_query = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", ascii_query.casefold())


def _project_embedding(vector: Any) -> list[int] | None:
    if not isinstance(vector, (list, tuple)) or not vector:
        return None
    buckets = [0.0] * AI_QUERY_PROJECTION_DIMENSIONS
    valid_values = 0
    for index, raw_value in enumerate(vector):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        mixed = (index + 1) * 2_654_435_761
        bucket = mixed % AI_QUERY_PROJECTION_DIMENSIONS
        sign = -1.0 if (mixed >> 16) & 1 else 1.0
        buckets[bucket] += sign * value
        valid_values += 1
    norm = math.sqrt(sum(value * value for value in buckets))
    if not valid_values or norm == 0:
        return None
    return [round((value / norm) * 10_000) for value in buckets]


def _projection_cosine(left: Any, right: Any) -> float:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return 0.0
    try:
        dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
        right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    except (TypeError, ValueError):
        return 0.0
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _fuzzy_term_coverage(left: Any, right: Any) -> float:
    if not isinstance(left, list) or not isinstance(right, list) or not left or not right:
        return 0.0
    unmatched = [str(value) for value in right]
    matched = 0
    for term in (str(value) for value in left):
        scored = [
            (SequenceMatcher(None, term, candidate).ratio(), index)
            for index, candidate in enumerate(unmatched)
        ]
        if not scored:
            continue
        score, index = max(scored)
        if score >= 0.80:
            matched += 1
            unmatched.pop(index)
    return matched / max(len(left), len(right))


class AIResponseCacheService:
    """Persist exact work and conservatively reusable research subproblems."""

    def __init__(
        self,
        session_factory=None,
        *,
        ttl_days: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.ttl_days = _ttl_days() if ttl_days is None else max(0, int(ttl_days))
        self.enabled = _cache_enabled() if enabled is None else bool(enabled)

    def _resolve_session_factory(self):
        if self._session_factory is not None:
            return self._session_factory
        if db_engine.SessionLocal is None:
            db_engine.init_engine()
        return db_engine.SessionLocal

    @staticmethod
    def scope_sha256(scope: str) -> str:
        normalized = str(scope or "").strip()
        if not normalized:
            raise ValueError("AI response cache requires a non-empty authorization scope")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def build_key(
        self,
        *,
        scope: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        prompt: str,
        namespace: str = AI_RESPONSE_CACHE_NAMESPACE,
    ) -> tuple[str, str]:
        scope_digest = self.scope_sha256(scope)
        canonical = json.dumps(
            {
                "cache_version": AI_RESPONSE_CACHE_VERSION,
                "namespace": namespace,
                "scope_sha256": scope_digest,
                "model": str(model or "").strip(),
                "schema_name": str(schema_name or "").strip(),
                "schema": schema,
                "prompt": prompt,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), scope_digest

    def build_semantic_key(
        self,
        *,
        scope: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        prompt_template: str,
        namespace: str = AI_RESPONSE_CACHE_NAMESPACE,
    ) -> str:
        """Group identical evidence contracts after removing only the query slot."""
        semantic_key, _scope_digest = self.build_key(
            scope=scope,
            model=model,
            schema_name=schema_name,
            schema=schema,
            prompt=prompt_template,
            namespace=f"{namespace}:semantic-query-v1",
        )
        return semantic_key

    @staticmethod
    def build_query_profile(
        query: str,
        query_embeddings: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Build a compact semantic profile from embeddings already paid for by search.

        Full embedding vectors and the raw request are deliberately not stored.
        A fixed 128-dimensional feature-hash projection supports approximate
        similarity, while lexical and logical guards fail closed on meaning
        changes that a vector score alone could hide.
        """
        tokens = _profile_tokens(query)
        core_terms = [
            token
            for token in tokens
            if token not in _QUERY_COMMAND_TERMS and token not in _QUERY_STOP_TERMS
        ]
        if not core_terms:
            return None
        embedding_model: str | None = None
        projection: list[int] | None = None
        for candidate_model in sorted(query_embeddings or {}):
            candidate_projection = _project_embedding((query_embeddings or {})[candidate_model])
            if candidate_projection is not None:
                embedding_model = str(candidate_model)
                projection = candidate_projection
                break
        if embedding_model is None or projection is None:
            return None
        return {
            "version": AI_QUERY_PROFILE_VERSION,
            "embedding_model": embedding_model,
            "projection": projection,
            "core_terms": core_terms,
            "logic_terms": sorted(token for token in tokens if token in _QUERY_LOGIC_TERMS),
            "numeric_terms": sorted(token for token in tokens if any(char.isdigit() for char in token)),
        }

    @staticmethod
    def compare_query_profiles(
        current: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify near-equivalence conservatively; similarity alone is never proof."""
        compatible = (
            current.get("version") == candidate.get("version") == AI_QUERY_PROFILE_VERSION
            and current.get("embedding_model") == candidate.get("embedding_model")
            and current.get("logic_terms") == candidate.get("logic_terms")
            and current.get("numeric_terms") == candidate.get("numeric_terms")
        )
        cosine = (
            _projection_cosine(current.get("projection"), candidate.get("projection"))
            if compatible
            else 0.0
        )
        term_coverage = (
            _fuzzy_term_coverage(current.get("core_terms"), candidate.get("core_terms"))
            if compatible
            else 0.0
        )
        return {
            "equivalent": bool(
                compatible
                and cosine >= AI_QUERY_EQUIVALENT_COSINE
                and term_coverage == 1.0
            ),
            "related": bool(
                compatible
                and cosine >= AI_QUERY_RELATED_COSINE
                and term_coverage >= 0.5
            ),
            "embedding_cosine": round(cosine, 6),
            "term_coverage": round(term_coverage, 6),
        }

    @staticmethod
    def _row_is_fresh(row: AIResponseCache, now: datetime) -> bool:
        expires_at = row.expires_at
        if expires_at is None:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at > now

    async def _find_profile_match(
        self,
        *,
        scope_sha256: str,
        namespace: str,
        model: str,
        schema_name: str,
        query_profile: dict[str, Any],
        semantic_key: str | None,
        match_kind: str,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        session_factory = self._resolve_session_factory()
        if session_factory is None:
            return None
        try:
            async with session_factory() as session:
                repo = AIResponseCacheRepo(session)
                candidates = await repo.list_semantic_candidates(
                    scope_sha256=scope_sha256,
                    namespace=namespace,
                    model=model,
                    schema_name=schema_name,
                    semantic_key=semantic_key,
                )
                now = datetime.now(UTC)
                matches: list[tuple[float, AIResponseCache, dict[str, Any]]] = []
                for row in candidates:
                    if not self._row_is_fresh(row, now) or not row.query_profile:
                        continue
                    comparison = self.compare_query_profiles(query_profile, row.query_profile)
                    if comparison.get(match_kind) is True:
                        matches.append((float(comparison["embedding_cosine"]), row, comparison))
                if not matches:
                    return None
                _score, row, comparison = max(matches, key=lambda item: item[0])
                await repo.record_hit(row, now=now)
                await session.commit()
                return {
                    "response": dict(row.response_payload),
                    "usage": dict(row.usage_payload),
                    "match": comparison,
                    "cache_key": row.cache_key,
                }
        except Exception as error:
            logger.warning(
                "AI response semantic-cache lookup failed; using provider",
                match_kind=match_kind,
                error=str(error),
            )
            return None

    async def get_semantic_equivalent(
        self,
        *,
        scope_sha256: str,
        model: str,
        schema_name: str,
        semantic_key: str,
        query_profile: dict[str, Any],
        namespace: str = AI_RESPONSE_CACHE_NAMESPACE,
    ) -> dict[str, Any] | None:
        return await self._find_profile_match(
            scope_sha256=scope_sha256,
            namespace=namespace,
            model=model,
            schema_name=schema_name,
            query_profile=query_profile,
            semantic_key=semantic_key,
            match_kind="equivalent",
        )

    async def get_related_research(
        self,
        *,
        scope_sha256: str,
        model: str,
        schema_name: str,
        query_profile: dict[str, Any],
        namespace: str = AI_RESPONSE_CACHE_NAMESPACE,
    ) -> dict[str, Any] | None:
        """Return related prior work as a discovery hint, never as an answer."""
        return await self._find_profile_match(
            scope_sha256=scope_sha256,
            namespace=namespace,
            model=model,
            schema_name=schema_name,
            query_profile=query_profile,
            semantic_key=None,
            match_kind="related",
        )

    async def get(self, cache_key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        session_factory = self._resolve_session_factory()
        if session_factory is None:
            return None
        try:
            async with session_factory() as session:
                row = await AIResponseCacheRepo(session).get_fresh(cache_key)
                if row is None:
                    return None
                result = {
                    "response": dict(row.response_payload),
                    "usage": dict(row.usage_payload),
                }
                await session.commit()
                return result
        except Exception as error:
            logger.warning("AI response cache read failed; using provider", error=str(error))
            return None

    async def put(
        self,
        *,
        cache_key: str,
        scope_sha256: str,
        model: str,
        schema_name: str,
        response: dict[str, Any],
        usage: dict[str, Any] | None,
        semantic_key: str | None = None,
        query_profile: dict[str, Any] | None = None,
        namespace: str = AI_RESPONSE_CACHE_NAMESPACE,
    ) -> None:
        if not self.enabled:
            return
        session_factory = self._resolve_session_factory()
        if session_factory is None:
            return
        now = datetime.now(UTC)
        row = AIResponseCache(
            cache_key=cache_key,
            scope_sha256=scope_sha256,
            namespace=namespace,
            model=str(model or "").strip(),
            schema_name=str(schema_name or "").strip(),
            semantic_key=semantic_key,
            query_profile=query_profile,
            response_payload=response,
            usage_payload=usage or {},
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=self.ttl_days) if self.ttl_days > 0 else None,
        )
        try:
            async with session_factory() as session:
                await AIResponseCacheRepo(session).put(row)
                await session.commit()
        except Exception as error:
            logger.warning("AI response cache write failed; result remains valid", error=str(error))


ai_response_cache_service = AIResponseCacheService()
