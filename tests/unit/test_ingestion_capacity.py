"""Dynamic ingestion capacity contracts.

These tests distinguish Docling RQ workers from backend/Uvicorn workers and
exercise worker churn, scrape failures, and safe limiter resizing.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from utils.ingestion_capacity import (
    DoclingWorkerCapacityMonitor,
    ResizableAsyncLimiter,
    load_ingestion_capacity_config,
    parse_docling_rq_worker_count,
    resolve_ingestion_capacity_config,
)


def _metrics(*workers: tuple[str, str, str, int]) -> str:
    return "\n".join(
        f'rq_workers{{name="{name}",queues="{queues}",state="{state}"}} {value}'
        for name, queues, state, value in workers
    )


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _SequenceClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    async def get(self, _url, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else _Response("")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self):
        self.closed = True


def _auto_config(**env_overrides):
    env = {
        "MAX_WORKERS": "auto",
        "MAX_WORKERS_FALLBACK": "2",
        "MAX_WORKERS_MIN": "1",
        "MAX_WORKERS_MAX": "6",
        "DOCLING_WORKER_REFRESH_SECONDS": "15",
        "DOCLING_WORKER_FAILURE_THRESHOLD": "3",
        **env_overrides,
    }
    return load_ingestion_capacity_config(env)


def test_manual_mode_preserves_integer_capacity():
    config = load_ingestion_capacity_config({"MAX_WORKERS": "9"})

    assert config.mode == "manual"
    assert config.manual_capacity == 9
    assert config.initial_capacity == 9


def test_workspace_mode_can_follow_deployment_or_override_it():
    deployment_env = {"MAX_WORKERS": "4"}

    assert (
        resolve_ingestion_capacity_config(
            SimpleNamespace(ingestion_concurrency_mode="deployment"), env=deployment_env
        ).initial_capacity
        == 4
    )
    assert (
        resolve_ingestion_capacity_config(
            SimpleNamespace(
                ingestion_concurrency_mode="manual",
                ingestion_manual_workers=3,
            ),
            env=deployment_env,
        ).initial_capacity
        == 3
    )


def test_workspace_automatic_maximum_is_bounded_by_deployment_minimum():
    config = resolve_ingestion_capacity_config(
        SimpleNamespace(
            ingestion_concurrency_mode="auto",
            ingestion_worker_fallback=1,
            ingestion_worker_max=1,
        ),
        env={
            "MAX_WORKERS": "auto",
            "MAX_WORKERS_MIN": "2",
            "MAX_WORKERS_MAX": "6",
        },
    )

    assert config.minimum == 2
    assert config.maximum == 2
    assert config.fallback == 2


def test_metrics_count_idle_and_busy_convert_workers_once():
    metrics = _metrics(
        ("permanent", "convert", "idle", 1),
        ("permanent", "convert", "busy", 0),
        ("opportunistic", "high,convert", "busy", 1),
        ("unrelated", "notifications", "idle", 1),
    )

    assert parse_docling_rq_worker_count(metrics) == 2


def test_metrics_exclude_non_convert_and_dead_series():
    metrics = _metrics(
        ("dead", "convert", "idle", 0),
        ("backend", "default", "busy", 1),
        ("scheduler", "conversion", "idle", 1),
    )

    assert parse_docling_rq_worker_count(metrics) == 0


@pytest.mark.asyncio
async def test_monitor_applies_minimum_and_maximum_bounds():
    config = replace(_auto_config(), minimum=2, maximum=4, fallback=2)
    client = _SequenceClient(
        _Response(_metrics(*((f"worker-{index}", "convert", "idle", 1) for index in range(7)))),
        _Response(_metrics(("only", "convert", "busy", 1))),
    )
    limiter = ResizableAsyncLimiter(config.initial_capacity)
    monitor = DoclingWorkerCapacityMonitor(limiter, config, http_client=client)

    await monitor.refresh_once()
    assert limiter.target_capacity == 4
    await monitor.refresh_once()
    assert limiter.target_capacity == 2


@pytest.mark.asyncio
async def test_metrics_unavailable_at_startup_uses_gitops_fallback():
    config = _auto_config()
    limiter = ResizableAsyncLimiter(config.initial_capacity)
    monitor = DoclingWorkerCapacityMonitor(
        limiter,
        config,
        http_client=_SequenceClient(RuntimeError("connection refused")),
    )

    await monitor.refresh_once()

    assert limiter.target_capacity == 2
    assert monitor.snapshot()["detection_state"] == "fallback"


@pytest.mark.asyncio
async def test_last_good_capacity_is_retained_then_falls_back_at_threshold():
    config = _auto_config()
    client = _SequenceClient(
        _Response(
            _metrics(
                ("one", "convert", "idle", 1),
                ("two", "convert", "busy", 1),
                ("three", "convert", "idle", 1),
                ("four", "convert", "idle", 1),
            )
        ),
        RuntimeError("temporary-1"),
        RuntimeError("temporary-2"),
        RuntimeError("temporary-3"),
    )
    limiter = ResizableAsyncLimiter(config.initial_capacity)
    monitor = DoclingWorkerCapacityMonitor(limiter, config, http_client=client)

    await monitor.refresh_once()
    assert limiter.target_capacity == 4
    await monitor.refresh_once()
    assert limiter.target_capacity == 4
    assert monitor.snapshot()["detection_state"] == "stale"
    await monitor.refresh_once()
    assert limiter.target_capacity == 4
    await monitor.refresh_once()
    assert limiter.target_capacity == 2
    assert monitor.snapshot()["detection_state"] == "fallback"


@pytest.mark.asyncio
async def test_zero_live_workers_uses_configured_fallback():
    config = _auto_config(MAX_WORKERS_FALLBACK="3")
    limiter = ResizableAsyncLimiter(config.initial_capacity)
    monitor = DoclingWorkerCapacityMonitor(
        limiter,
        config,
        http_client=_SequenceClient(_Response(_metrics(("gone", "convert", "idle", 0)))),
    )

    await monitor.refresh_once()

    assert limiter.target_capacity == 3
    assert monitor.snapshot()["detected_workers"] == 0
    assert monitor.snapshot()["detection_state"] == "fallback"


@pytest.mark.asyncio
async def test_limiter_increase_wakes_waiting_work():
    limiter = ResizableAsyncLimiter(1)
    await limiter.acquire()
    acquired = asyncio.Event()

    async def wait_for_slot():
        async with limiter:
            acquired.set()

    waiter = asyncio.create_task(wait_for_slot())
    await asyncio.sleep(0)
    assert not acquired.is_set()

    await limiter.resize(2)
    await asyncio.wait_for(acquired.wait(), timeout=0.2)
    await waiter
    await limiter.release()
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_limiter_decrease_drains_without_cancelling_active_work():
    limiter = ResizableAsyncLimiter(2)
    await limiter.acquire()
    await limiter.acquire()
    third_acquired = asyncio.Event()

    async def third():
        async with limiter:
            third_acquired.set()

    waiter = asyncio.create_task(third())
    await limiter.resize(1)
    await limiter.release()
    await asyncio.sleep(0)
    assert not third_acquired.is_set()
    assert limiter.active == 1

    await limiter.release()
    await asyncio.wait_for(third_acquired.wait(), timeout=0.2)
    await waiter
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_limiter_releases_slots_after_exception_and_cancellation():
    limiter = ResizableAsyncLimiter(1)

    with pytest.raises(RuntimeError, match="boom"):
        async with limiter:
            raise RuntimeError("boom")
    assert limiter.active == 0

    entered = asyncio.Event()
    never = asyncio.Event()

    async def cancelled_holder():
        async with limiter:
            entered.set()
            await never.wait()

    task = asyncio.create_task(cancelled_holder())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_monitor_stops_while_waiting_for_next_refresh():
    config = replace(_auto_config(), refresh_seconds=3600)
    client = _SequenceClient(_Response(_metrics(("one", "convert", "idle", 1))))
    monitor = DoclingWorkerCapacityMonitor(
        ResizableAsyncLimiter(config.initial_capacity),
        config,
        http_client=client,
    )

    monitor.start()
    for _ in range(100):
        if client.calls:
            break
        await asyncio.sleep(0.005)
    assert client.calls == 1
    assert monitor.running

    await monitor.stop()

    assert not monitor.running
    assert client.closed is False
