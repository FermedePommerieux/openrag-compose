"""Content-addressed cache for repeated, structured AI verification calls.

Truth and authorization are part of the cache contract:

* the caller supplies a stable user/ACL scope;
* the digest includes the complete evidence prompt, model and JSON schema;
* source text changes therefore produce a different key automatically;
* a cache failure is fail-open and never prevents a fresh provider request.

The service is intentionally limited to validated structured responses.  It
does not cache final prose answers, whose wording and conversational context
may change independently of the documentary evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from db import engine as db_engine
from db.models.ai_response_cache import AIResponseCache
from db.repositories.ai_response_cache_repo import AIResponseCacheRepo
from utils.logging_config import get_logger

logger = get_logger(__name__)

AI_RESPONSE_CACHE_VERSION = "1"
AI_RESPONSE_CACHE_NAMESPACE = "audit_structured_response"
# Documentary proofs are durable research work, not an ephemeral HTTP cache.
# Zero therefore means "no expiry"; disabling storage is a separate flag so
# an operator cannot accidentally discard reusable evidence by selecting an
# unlimited retention policy.
DEFAULT_AI_RESPONSE_CACHE_TTL_DAYS = 0


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


class AIResponseCacheService:
    """Read and write exact structured-call results in the backend database."""

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
