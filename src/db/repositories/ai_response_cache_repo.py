"""Persistence operations for the structured AI response cache."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

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
