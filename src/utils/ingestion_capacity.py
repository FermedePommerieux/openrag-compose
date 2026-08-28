"""Dynamic concurrency control for backend-owned ingestion work.

This module deliberately counts Docling RQ workers, not Uvicorn processes or
Kubernetes replicas. OpenRAG keeps one backend process for its in-memory RBAC
caches while independently matching file-ingestion concurrency to workers on
the RQ ``convert`` queue.
"""

import asyncio
import json
import multiprocessing
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from utils.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_DOCLING_METRICS_URL = "http://docling-serve.docling.svc.cluster.local:5001/metrics"
_RQ_WORKER_LINE = re.compile(
    r"^rq_workers(?:\{(?P<labels>.*)\})?\s+(?P<value>[-+0-9.eE]+)(?:\s+\d+)?$"
)
_PROMETHEUS_LABEL = re.compile(r'(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')


@dataclass(frozen=True, slots=True)
class IngestionCapacityConfig:
    """Validated ingestion capacity settings loaded once at backend startup."""

    mode: Literal["auto", "manual"]
    initial_capacity: int
    fallback: int
    minimum: int
    maximum: int
    metrics_url: str
    refresh_seconds: float
    failure_threshold: int
    manual_capacity: int | None = None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def load_ingestion_capacity_config(
    env: Mapping[str, str] | None = None,
) -> IngestionCapacityConfig:
    """Load manual or Docling-driven ingestion concurrency settings.

    An absent ``MAX_WORKERS`` preserves the historic host-CPU default. The
    explicit value ``auto`` enables RQ discovery. In auto mode the initial
    capacity is the bounded GitOps fallback, so startup never depends on the
    metrics endpoint being immediately reachable.
    """
    source = os.environ if env is None else env
    legacy_default = max(1, min(4, multiprocessing.cpu_count() // 2))
    raw_max_workers = source.get("MAX_WORKERS", "").strip()

    minimum = _positive_int(source, "MAX_WORKERS_MIN", 1)
    maximum = _positive_int(source, "MAX_WORKERS_MAX", 6)
    if minimum > maximum:
        raise ValueError(f"MAX_WORKERS_MIN ({minimum}) must not exceed MAX_WORKERS_MAX ({maximum})")
    configured_fallback = _positive_int(source, "MAX_WORKERS_FALLBACK", 2)
    fallback = min(maximum, max(minimum, configured_fallback))

    refresh_raw = source.get("DOCLING_WORKER_REFRESH_SECONDS", "15").strip()
    try:
        refresh_seconds = float(refresh_raw)
    except ValueError as exc:
        raise ValueError(
            f"DOCLING_WORKER_REFRESH_SECONDS must be a positive number, got {refresh_raw!r}"
        ) from exc
    if refresh_seconds <= 0:
        raise ValueError("DOCLING_WORKER_REFRESH_SECONDS must be greater than 0")

    failure_threshold = _positive_int(source, "DOCLING_WORKER_FAILURE_THRESHOLD", 3)
    metrics_url = source.get("DOCLING_METRICS_URL", DEFAULT_DOCLING_METRICS_URL).strip()
    if not metrics_url:
        raise ValueError("DOCLING_METRICS_URL must not be empty")

    if raw_max_workers.casefold() == "auto":
        return IngestionCapacityConfig(
            mode="auto",
            initial_capacity=fallback,
            fallback=fallback,
            minimum=minimum,
            maximum=maximum,
            metrics_url=metrics_url,
            refresh_seconds=refresh_seconds,
            failure_threshold=failure_threshold,
        )

    if raw_max_workers:
        try:
            manual_capacity = max(1, int(raw_max_workers))
        except ValueError as exc:
            raise ValueError(
                f"MAX_WORKERS must be 'auto' or an integer, got {raw_max_workers!r}"
            ) from exc
    else:
        manual_capacity = legacy_default

    return IngestionCapacityConfig(
        mode="manual",
        initial_capacity=manual_capacity,
        manual_capacity=manual_capacity,
        fallback=fallback,
        minimum=minimum,
        maximum=maximum,
        metrics_url=metrics_url,
        refresh_seconds=refresh_seconds,
        failure_threshold=failure_threshold,
    )


def resolve_ingestion_capacity_config(
    knowledge_config: Any | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> IngestionCapacityConfig:
    """Apply an optional persisted UI override to deployment defaults.

    ``deployment`` (and legacy configurations without the field) keeps Helm or
    environment settings authoritative. Explicit ``auto`` and ``manual`` UI
    choices are persisted with the other Knowledge/Ingestion settings and take
    effect immediately as well as after restart. Metrics URL, refresh cadence,
    minimum and failure policy remain deployment-owned.
    """
    deployment = load_ingestion_capacity_config(env)
    mode = getattr(knowledge_config, "ingestion_concurrency_mode", "deployment")
    if mode not in {"auto", "manual"}:
        return deployment

    if mode == "manual":
        manual = getattr(knowledge_config, "ingestion_manual_workers", None)
        if not isinstance(manual, int) or isinstance(manual, bool) or manual < 1:
            manual = deployment.initial_capacity
        return IngestionCapacityConfig(
            mode="manual",
            initial_capacity=manual,
            manual_capacity=manual,
            fallback=deployment.fallback,
            minimum=deployment.minimum,
            maximum=deployment.maximum,
            metrics_url=deployment.metrics_url,
            refresh_seconds=deployment.refresh_seconds,
            failure_threshold=deployment.failure_threshold,
        )

    configured_maximum = getattr(knowledge_config, "ingestion_worker_max", None)
    maximum = (
        max(deployment.minimum, configured_maximum)
        if isinstance(configured_maximum, int)
        and not isinstance(configured_maximum, bool)
        and configured_maximum >= 1
        else deployment.maximum
    )
    configured_fallback = getattr(knowledge_config, "ingestion_worker_fallback", None)
    fallback = (
        configured_fallback
        if isinstance(configured_fallback, int)
        and not isinstance(configured_fallback, bool)
        and configured_fallback >= 1
        else deployment.fallback
    )
    fallback = min(maximum, max(deployment.minimum, fallback))
    return IngestionCapacityConfig(
        mode="auto",
        initial_capacity=fallback,
        fallback=fallback,
        minimum=deployment.minimum,
        maximum=maximum,
        metrics_url=deployment.metrics_url,
        refresh_seconds=deployment.refresh_seconds,
        failure_threshold=deployment.failure_threshold,
    )


def _parse_prometheus_labels(raw_labels: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in _PROMETHEUS_LABEL.finditer(raw_labels):
        raw_value = match.group("value")
        try:
            labels[match.group("name")] = json.loads(f'"{raw_value}"')
        except json.JSONDecodeError:
            labels[match.group("name")] = raw_value
    return labels


def _queue_contains_convert(queue_label: str) -> bool:
    return "convert" in re.findall(r"[a-zA-Z0-9_.:-]+", queue_label)


def parse_docling_rq_worker_count(metrics_text: str) -> int:
    """Count unique live RQ workers subscribed to the ``convert`` queue.

    Both idle and busy workers count. Exporters commonly expose one series per
    state with zeroes for inactive states, so the worker ``name`` label is
    deduplicated and only positive-valued series are considered live.
    """
    workers: set[str] = set()
    for line_number, raw_line in enumerate(metrics_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _RQ_WORKER_LINE.match(line)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if value <= 0:
            continue
        labels = _parse_prometheus_labels(match.group("labels") or "")
        if not _queue_contains_convert(labels.get("queues", "")):
            continue
        workers.add(labels.get("name") or f"unnamed-series-{line_number}")
    return len(workers)


class ResizableAsyncLimiter:
    """Cancellation-safe async capacity limiter with hot resizing.

    Reducing the target never cancels work already holding a slot. New
    acquisitions wait while ``active >= target`` and resume naturally as work
    completes. No private ``asyncio.Semaphore`` internals are inspected or
    mutated.
    """

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._target_capacity = capacity
        self._active = 0
        self._waiting = 0
        self._condition = asyncio.Condition()

    @property
    def target_capacity(self) -> int:
        return self._target_capacity

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    async def resize(self, capacity: int) -> int:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        async with self._condition:
            previous = self._target_capacity
            self._target_capacity = capacity
            if capacity > previous:
                self._condition.notify_all()
            return previous

    async def acquire(self) -> None:
        async with self._condition:
            self._waiting += 1
            try:
                await self._condition.wait_for(lambda: self._active < self._target_capacity)
                self._active += 1
            finally:
                self._waiting -= 1

    async def release(self) -> None:
        async with self._condition:
            if self._active < 1:
                raise RuntimeError("ingestion capacity released without an active slot")
            self._active -= 1
            self._condition.notify_all()

    async def __aenter__(self) -> "ResizableAsyncLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.release()


class DoclingWorkerCapacityMonitor:
    """Resize ingestion capacity from the live Docling RQ worker topology."""

    HTTP_TIMEOUT_SECONDS = 3.0

    def __init__(
        self,
        limiter: ResizableAsyncLimiter,
        config: IngestionCapacityConfig,
        *,
        http_client: Any | None = None,
    ):
        if config.mode != "auto":
            raise ValueError("Docling worker monitoring requires MAX_WORKERS=auto")
        self._limiter = limiter
        self._config = config
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._task: asyncio.Task[None] | None = None
        self._detected_workers: int | None = None
        self._last_valid_workers: int | None = None
        self._detection_state: Literal["healthy", "stale", "fallback"] = "fallback"
        self._consecutive_failures = 0
        self._last_success_at: float | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": "auto",
            "effective_capacity": self._limiter.target_capacity,
            "active": self._limiter.active,
            "waiting": self._limiter.waiting,
            "detected_workers": self._detected_workers,
            "detection_state": self._detection_state,
            "consecutive_failures": self._consecutive_failures,
            "last_success_at": self._last_success_at,
        }

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="docling-worker-capacity-monitor")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.HTTP_TIMEOUT_SECONDS),
                follow_redirects=False,
            )
        return self._http_client

    async def _set_capacity(
        self,
        capacity: int,
        *,
        detected_workers: int | None,
        fallback_reason: str | None = None,
    ) -> None:
        previous = await self._limiter.resize(capacity)
        if previous != capacity:
            logger.info(
                "Ingestion capacity changed",
                old_capacity=previous,
                new_capacity=capacity,
                detected_workers=detected_workers,
                mode="auto",
                fallback_reason=fallback_reason,
            )

    async def refresh_once(self) -> None:
        try:
            client = await self._client()
            response = await client.get(
                self._config.metrics_url,
                timeout=self.HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            detected_workers = parse_docling_rq_worker_count(response.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_failure(exc)
            return

        recovered = self._consecutive_failures > 0 or self._detection_state != "healthy"
        self._consecutive_failures = 0
        self._detected_workers = detected_workers
        self._last_success_at = time.time()

        if detected_workers < 1:
            self._detection_state = "fallback"
            await self._set_capacity(
                self._config.fallback,
                detected_workers=0,
                fallback_reason="no_live_convert_workers",
            )
            if recovered:
                logger.warning(
                    "Docling metrics reported no live convert workers; using fallback",
                    detected_workers=0,
                    fallback_capacity=self._config.fallback,
                    mode="auto",
                )
            return

        self._last_valid_workers = detected_workers
        self._detection_state = "healthy"
        capacity = min(
            self._config.maximum,
            max(self._config.minimum, detected_workers),
        )
        await self._set_capacity(capacity, detected_workers=detected_workers)
        if recovered:
            logger.info(
                "Docling worker detection healthy",
                detected_workers=detected_workers,
                effective_capacity=capacity,
                mode="auto",
            )

    async def _record_failure(self, exc: Exception) -> None:
        self._consecutive_failures += 1
        threshold_reached = self._consecutive_failures >= self._config.failure_threshold

        if self._last_valid_workers is not None and not threshold_reached:
            self._detection_state = "stale"
            if self._consecutive_failures == 1:
                logger.warning(
                    "Docling worker metrics temporarily unavailable; retaining last capacity",
                    error=str(exc),
                    consecutive_failures=self._consecutive_failures,
                    failure_threshold=self._config.failure_threshold,
                    effective_capacity=self._limiter.target_capacity,
                    mode="auto",
                )
            return

        self._detection_state = "fallback"
        await self._set_capacity(
            self._config.fallback,
            detected_workers=self._detected_workers,
            fallback_reason="metrics_unavailable",
        )
        if self._consecutive_failures == 1 or (
            self._consecutive_failures == self._config.failure_threshold
        ):
            logger.warning(
                "Docling worker metrics unavailable; using fallback capacity",
                error=str(exc),
                consecutive_failures=self._consecutive_failures,
                failure_threshold=self._config.failure_threshold,
                fallback_capacity=self._config.fallback,
                mode="auto",
            )

    async def _run(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self._config.refresh_seconds)
