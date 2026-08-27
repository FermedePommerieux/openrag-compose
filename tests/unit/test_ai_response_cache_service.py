from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from db.models.ai_response_cache import AIResponseCache
from db.repositories.ai_response_cache_repo import AIResponseCacheRepo
from services.ai_response_cache_service import AIResponseCacheService


def test_structured_cache_key_is_deterministic_and_scope_bound() -> None:
    service = AIResponseCacheService(ttl_days=30)
    request: dict[str, Any] = {
        "model": "gpt-5.6-luna",
        "schema_name": "finding",
        "schema": {"type": "object", "properties": {"value": {"type": "string"}}},
        "prompt": "Evidence digest 123",
    }

    first, first_scope = service.build_key(scope="user-a", **request)
    repeated, repeated_scope = service.build_key(scope="user-a", **request)
    other_user, other_scope = service.build_key(scope="user-b", **request)

    assert (first, first_scope) == (repeated, repeated_scope)
    assert first != other_user
    assert first_scope != other_scope


def test_structured_cache_key_changes_with_evidence_or_schema() -> None:
    service = AIResponseCacheService(ttl_days=30)
    base: dict[str, Any] = {
        "scope": "user-a",
        "model": "gpt-5.6-luna",
        "schema_name": "finding",
        "schema": {"type": "object"},
        "prompt": "Evidence A",
    }
    first, _ = service.build_key(**base)
    changed_evidence, _ = service.build_key(**{**base, "prompt": "Evidence B"})
    changed_schema, _ = service.build_key(
        **{**base, "schema": {"type": "object", "required": []}}
    )

    assert len(first) == 64
    assert len({first, changed_evidence, changed_schema}) == 3


def test_structured_cache_refuses_unscoped_entries() -> None:
    service = AIResponseCacheService(ttl_days=30)

    with pytest.raises(ValueError, match="authorization scope"):
        service.build_key(
            scope="",
            model="gpt-5.6-luna",
            schema_name="finding",
            schema={},
            prompt="evidence",
        )


@pytest.mark.asyncio
async def test_structured_cache_round_trips_validated_payload() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    service = AIResponseCacheService(session_factory=session_factory, ttl_days=30)
    cache_key, scope_sha256 = service.build_key(
        scope="user-a",
        model="gpt-5.6-luna",
        schema_name="finding",
        schema={"type": "object"},
        prompt="Evidence A",
    )

    await service.put(
        cache_key=cache_key,
        scope_sha256=scope_sha256,
        model="gpt-5.6-luna",
        schema_name="finding",
        response={"value": "verified"},
        usage={"input_tokens": 100, "output_tokens": 5, "cost_usd": 0.00003},
    )

    assert await service.get(cache_key) == {
        "response": {"value": "verified"},
        "usage": {"input_tokens": 100, "output_tokens": 5, "cost_usd": 0.00003},
    }
    await engine.dispose()


def test_structured_cache_defaults_to_unlimited_retention(monkeypatch) -> None:
    monkeypatch.delenv("OPENRAG_AI_RESPONSE_CACHE_TTL_DAYS", raising=False)
    monkeypatch.delenv("OPENRAG_AI_RESPONSE_CACHE_ENABLED", raising=False)

    service = AIResponseCacheService()

    assert service.enabled is True
    assert service.ttl_days == 0


@pytest.mark.asyncio
async def test_unlimited_cache_stores_null_expiry_and_remains_readable() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    service = AIResponseCacheService(
        session_factory=session_factory,
        ttl_days=0,
    )
    cache_key, scope_sha256 = service.build_key(
        scope="user-a",
        model="gpt-5.6-luna",
        schema_name="finding",
        schema={"type": "object"},
        prompt="Durable evidence",
    )

    await service.put(
        cache_key=cache_key,
        scope_sha256=scope_sha256,
        model="gpt-5.6-luna",
        schema_name="finding",
        response={"value": "verified"},
        usage=None,
    )

    async with session_factory() as session:
        stored = await session.get(AIResponseCache, cache_key)
        assert stored is not None
        assert stored.expires_at is None
    assert await service.get(cache_key) == {
        "response": {"value": "verified"},
        "usage": {},
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_positive_retention_still_rejects_expired_entries() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    row = AIResponseCache(
        cache_key="expired",
        scope_sha256="a" * 64,
        namespace="audit_structured_response",
        model="gpt-5.6-luna",
        schema_name="finding",
        response_payload={"value": "old"},
        usage_payload={},
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    async with session_factory() as session:
        await AIResponseCacheRepo(session).put(row)
        await session.commit()
    service = AIResponseCacheService(
        session_factory=session_factory,
        ttl_days=30,
    )

    assert await service.get("expired") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cache_can_be_disabled_without_changing_retention_policy() -> None:
    service = AIResponseCacheService(
        session_factory=lambda: pytest.fail("disabled cache touched the database"),
        ttl_days=0,
        enabled=False,
    )

    assert await service.get("unused") is None
    await service.put(
        cache_key="unused",
        scope_sha256="a" * 64,
        model="gpt-5.6-luna",
        schema_name="finding",
        response={"value": "unused"},
        usage=None,
    )
