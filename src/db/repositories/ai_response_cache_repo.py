"""Persistence operations for the structured AI response cache."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from db.models.ai_response_cache import AIResponseCache


class AIResponseCacheRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_fresh(self, cache_key: str, *, now: datetime | None = None) -> AIResponseCache | None:
        row = await self.session.get(AIResponseCache, cache_key)
        if row is None:
            return None
        effective_now = now or datetime.now(UTC)
        expires_at = row.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= effective_now:
                return None
        row.hit_count += 1
        row.updated_at = effective_now
        self.session.add(row)
        await self.session.flush()
        return row

    async def put(self, row: AIResponseCache) -> None:
        await self.session.merge(row)
        await self.session.flush()

    async def list_semantic_candidates(
        self,
        *,
        scope_sha256: str,
        namespace: str,
        model: str,
        schema_name: str,
        semantic_key: str | None = None,
        limit: int | None = None,
    ) -> list[AIResponseCache]:
        """Return recent compatible rows; similarity is checked by the service."""
        statement = (
            select(AIResponseCache)
            .where(
                col(AIResponseCache.scope_sha256) == scope_sha256,
                col(AIResponseCache.namespace) == namespace,
                col(AIResponseCache.model) == model,
                col(AIResponseCache.schema_name) == schema_name,
                col(AIResponseCache.query_profile).is_not(None),
            )
            .order_by(col(AIResponseCache.updated_at).desc())
        )
        if semantic_key is not None:
            statement = statement.where(col(AIResponseCache.semantic_key) == semantic_key)
        if limit is not None:
            statement = statement.limit(max(1, int(limit)))
        result = await self.session.execute(statement)
        return list(result.scalars())

    async def record_hit(
        self,
        row: AIResponseCache,
        *,
        now: datetime | None = None,
    ) -> None:
        row.hit_count += 1
        row.updated_at = now or datetime.now(UTC)
        self.session.add(row)
        await self.session.flush()
