"""Best-effort, factual progress reporting for long archive audits.

Archive audits deliberately trade latency for coverage and verification.  The
chat stream therefore needs small, non-evidentiary status events while the
Langflow tool is waiting for the backend.  This registry carries only phase
codes and integer counters; document text, filenames, queries, credentials and
reasoning traces must never be stored here or emitted to the browser.

The registry is process-local by design.  OpenRAG currently runs one backend
replica, and progress is an optional observability aid rather than part of the
audit certificate.  A missing event can never change retrieval or answer
semantics.  If backend replicas are added, replace this implementation with a
shared TTL store while keeping the same public snapshot contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

AUDIT_PROGRESS_TTL = timedelta(hours=1)


@dataclass
class _AuditProgress:
    audit_id: str
    phase: str
    message: str
    sequence: int = 1
    counters: dict[str, int] = field(default_factory=dict)
    complete: bool = False
    failed: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def snapshot(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "phase": self.phase,
            "message": self.message,
            "sequence": self.sequence,
            "counters": dict(self.counters),
            "complete": self.complete,
            "failed": self.failed,
            "updated_at": self.updated_at.isoformat(),
        }


class AuditProgressService:
    """Store sanitized audit progress without affecting audit execution."""

    def __init__(self) -> None:
        self._items: dict[str, _AuditProgress] = {}
        self._lock = Lock()

    @staticmethod
    def _normalized_id(audit_id: str | None) -> str:
        value = str(audit_id or "").strip()
        # Chat creates a UUID hex token.  Bounding and restricting the value
        # prevents an untrusted API caller from turning registry keys into log
        # or memory payloads.
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
            # bool is intentionally rejected even though it subclasses int.
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            safe[normalized_key] = value
        return safe

    def _prune_locked(self, now: datetime) -> None:
        cutoff = now - AUDIT_PROGRESS_TTL
        expired = [key for key, item in self._items.items() if item.updated_at < cutoff]
        for key in expired:
            self._items.pop(key, None)

    def start(self, audit_id: str | None) -> None:
        normalized = self._normalized_id(audit_id)
        if not normalized:
            return
        now = datetime.now(UTC)
        with self._lock:
            self._prune_locked(now)
            self._items[normalized] = _AuditProgress(
                audit_id=normalized,
                phase="preparing",
                message="Preparing exhaustive archive audit",
                updated_at=now,
            )

    def update(
        self,
        audit_id: str | None,
        *,
        phase: str,
        message: str,
        counters: dict[str, Any] | None = None,
    ) -> None:
        normalized = self._normalized_id(audit_id)
        if not normalized:
            return
        now = datetime.now(UTC)
        with self._lock:
            item = self._items.get(normalized)
            if item is None:
                item = _AuditProgress(
                    audit_id=normalized,
                    phase="preparing",
                    message="Preparing exhaustive archive audit",
                    updated_at=now,
                )
                self._items[normalized] = item
            item.phase = str(phase or "working")[:64]
            item.message = str(message or "Audit in progress")[:160]
            item.counters.update(self._safe_counters(counters))
            item.sequence += 1
            item.updated_at = now

    def finish(self, audit_id: str | None, *, verified: bool) -> None:
        self.update(
            audit_id,
            phase="complete" if verified else "incomplete",
            message=(
                "Exhaustive audit completed and verified"
                if verified
                else "Audit stopped without a complete verification certificate"
            ),
        )
        normalized = self._normalized_id(audit_id)
        if not normalized:
            return
        with self._lock:
            item = self._items.get(normalized)
            if item is not None:
                item.complete = True
                item.failed = not verified
                item.sequence += 1
                item.updated_at = datetime.now(UTC)

    def fail(self, audit_id: str | None) -> None:
        self.update(
            audit_id,
            phase="failed",
            message="Audit failed before verification completed",
        )
        normalized = self._normalized_id(audit_id)
        if not normalized:
            return
        with self._lock:
            item = self._items.get(normalized)
            if item is not None:
                item.complete = True
                item.failed = True
                item.sequence += 1
                item.updated_at = datetime.now(UTC)

    def snapshot(self, audit_id: str | None) -> dict[str, Any] | None:
        normalized = self._normalized_id(audit_id)
        if not normalized:
            return None
        now = datetime.now(UTC)
        with self._lock:
            self._prune_locked(now)
            item = self._items.get(normalized)
            return item.snapshot() if item is not None else None


audit_progress_service = AuditProgressService()
