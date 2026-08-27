import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from services.ai_response_cache_service import AIResponseCacheService


def test_structured_cache_key_is_deterministic_and_scope_bound() -> None:
    service = AIResponseCacheService(ttl_days=30)
    request = {
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
    base = {
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
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
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
