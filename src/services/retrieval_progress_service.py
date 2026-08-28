"""Best-effort, factual progress for backend-owned document retrieval.

The registry contains only phase codes and integer counters. It never stores
queries, document text, filenames, credentials, or model reasoning. Progress
is observability, not evidence: a missing event cannot change retrieval output
or a coverage certificate.

OpenRAG currently runs one backend process, so a small process-local TTL store
is sufficient. A shared store can replace it behind this interface if backend
replicas are enabled later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

RETRIEVAL_PROGRESS_TTL = timedelta(hours=1)


@dataclass
class _RetrievalProgress:
    progress_id: str
    phase: str
    message: str
    sequence: int = 1
    counters: dict[str, int] = field(default_factory=dict)
    complete: bool = False
    failed: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def snapshot(self) -> dict[str, Any]:
        return {
            "progress_id": self.progress_id,
            "phase": self.phase,
            "message": self.message,
            "sequence": self.sequence,
            "counters": dict(self.counters),
            "complete": self.complete,
            "failed": self.failed,
            "updated_at": self.updated_at.isoformat(),
        }


class RetrievalProgressService:
    """Store sanitized retrieval progress without affecting execution."""

    def __init__(self) -> None:
        self._items: dict[str, _RetrievalProgress] = {}
        self._lock = Lock()

    @staticmethod
    def _normalized_id(progress_id: str | None) -> str:
        value = str(progress_id or "").strip()
        if not value or len(value) > 64 or not value.replace("-", "").isalnum():
            return ""
        return value

    @staticmethod
    def _safe_counters(counters: dict[str, Any] | None) -> dict[str, int]:
        safe: dict[str, int] = {}
        for key, value in (counters or {}).items():
            normalized_key = str(key or "").strip()
            if not normalized_key or len(normalized_key) > 64:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            safe[normalized_key] = value
        return safe

    def _prune_locked(self, now: datetime) -> None:
        cutoff = now - RETRIEVAL_PROGRESS_TTL
        for key in [key for key, item in self._items.items() if item.updated_at < cutoff]:
            self._items.pop(key, None)

    def start(self, progress_id: str | None) -> None:
        normalized = self._normalized_id(progress_id)
        if not normalized:
            return
        now = datetime.now(UTC)
        with self._lock:
            self._prune_locked(now)
            self._items[normalized] = _RetrievalProgress(
                progress_id=normalized,
                phase="preparing",
                message="Preparing document retrieval",
                updated_at=now,
            )

    def update(
        self,
        progress_id: str | None,
        *,
        phase: str,
        message: str,
        counters: dict[str, Any] | None = None,
    ) -> None:
        normalized = self._normalized_id(progress_id)
        if not normalized:
            return
        now = datetime.now(UTC)
        with self._lock:
            item = self._items.get(normalized)
            if item is None:
                item = _RetrievalProgress(
                    progress_id=normalized,
                    phase="preparing",
                    message="Preparing document retrieval",
                    updated_at=now,
                )
                self._items[normalized] = item
            item.phase = str(phase or "working")[:64]
            item.message = str(message or "Retrieval in progress")[:160]
            item.counters.update(self._safe_counters(counters))
            item.sequence += 1
            item.updated_at = now

    def finish(self, progress_id: str | None, *, complete: bool) -> None:
        self.update(
            progress_id,
            phase="complete" if complete else "incomplete",
            message=(
                "Document retrieval completed"
                if complete
                else "Document retrieval ended without complete coverage"
            ),
        )
        normalized = self._normalized_id(progress_id)
        if not normalized:
            return
        with self._lock:
            item = self._items.get(normalized)
            if item is not None:
                item.complete = True
                item.failed = not complete
                item.sequence += 1
                item.updated_at = datetime.now(UTC)

    def fail(self, progress_id: str | None) -> None:
        self.update(
            progress_id,
            phase="failed",
            message="Document retrieval failed",
        )
        normalized = self._normalized_id(progress_id)
        if not normalized:
            return
        with self._lock:
            item = self._items.get(normalized)
            if item is not None:
                item.complete = True
                item.failed = True
                item.sequence += 1
                item.updated_at = datetime.now(UTC)

    def snapshot(self, progress_id: str | None) -> dict[str, Any] | None:
        normalized = self._normalized_id(progress_id)
        if not normalized:
            return None
        now = datetime.now(UTC)
        with self._lock:
            self._prune_locked(now)
            item = self._items.get(normalized)
            return item.snapshot() if item is not None else None


retrieval_progress_service = RetrievalProgressService()
