#!/usr/bin/env python3
"""Run the approved historical metadata backfill in resumable canary-sized batches."""

from __future__ import annotations

import bootstrap  # noqa: F401  # load .env and expose the source tree

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import clients, get_index_name, get_indexed_documents_path
from services.document_metadata_backfill import (
    ArchivedOriginalResolver,
    IndexedDocumentRecord,
    MetadataBackfillStatus,
    load_archive_manifest,
    map_archived_original,
    scan_indexed_documents,
)
from services.document_metadata_canary import (
    CanaryCheckpoint,
    CanaryStatus,
    DocumentMetadataCanary,
)

WRITE_CONFIRMATION = "openrag.document-metadata-full-backfill-v1"
EXPECTED_TERMINAL_STATES = {
    CanaryStatus.VERIFIED.value,
    CanaryStatus.FAILED.value,
    CanaryStatus.DLS_BLOCKED.value,
    CanaryStatus.ARCHIVE_UNAVAILABLE.value,
}
TRANSIENT_FAILURE_PREFIXES = (
    "pre_write_read_failed:Connection",
    "pre_write_read_failed:Timeout",
    "opensearch_write_failed:Connection",
    "opensearch_write_failed:Timeout",
)


def _atomic_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _append_private_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _record_identity(record: IndexedDocumentRecord) -> dict[str, str]:
    return {
        "storage_id": record.storage_id,
        "document_id": record.document_id,
        "occurrence_id": record.occurrence_id,
    }


def _batch_checkpoint_path(root: Path, batch_index: int) -> Path:
    return root / "batches" / f"batch-{batch_index:05d}.json"


def _item_terminal_class(item: dict[str, Any]) -> str | None:
    state = str(item.get("state") or "")
    if state == CanaryStatus.VERIFIED.value:
        return "VERIFIED" if item.get("changed") is True else "UNCHANGED"
    if state in {
        CanaryStatus.FAILED.value,
        CanaryStatus.DLS_BLOCKED.value,
        CanaryStatus.ARCHIVE_UNAVAILABLE.value,
    }:
        return state
    return None


def _aggregate(checkpoint_root: Path, batch_count: int) -> dict[str, Any]:
    states: Counter[str] = Counter()
    bytes_read = writes = 0
    formats: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    timing_values: dict[str, list[float]] = defaultdict(list)
    processed = 0
    for batch_index in range(batch_count):
        path = _batch_checkpoint_path(checkpoint_root, batch_index)
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("items", {}).values():
            terminal = _item_terminal_class(item)
            if terminal is None:
                continue
            processed += 1
            states[terminal] += 1
            bytes_read += int(item.get("bytes_read") or 0)
            if item.get("format"):
                formats[str(item["format"])] += 1
            if terminal in {"FAILED", "DLS_BLOCKED", "ARCHIVE_UNAVAILABLE"}:
                reasons[str(item.get("reason") or "unspecified")] += 1
            history_states = {str(value.get("state")) for value in item.get("history", [])}
            if CanaryStatus.WRITTEN.value in history_states:
                writes += 1
            for phase, value in (item.get("timings") or {}).items():
                timing_values[str(phase)].append(float(value))
    return {
        "processed": processed,
        "states": dict(sorted(states.items())),
        "bytes_read": bytes_read,
        "opensearch_writes": writes,
        "formats": dict(sorted(formats.items())),
        "failure_reasons": dict(sorted(reasons.items())),
        "timing_values": timing_values,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def _timing_summary(values_by_phase: dict[str, list[float]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for phase, values in sorted(values_by_phase.items()):
        result[phase] = {
            "samples": len(values),
            "mean_ms": sum(values) / len(values),
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "p99_ms": _percentile(values, 0.99),
            "max_ms": max(values),
        }
    return result


class CountingClient:
    """Count OpenSearch I/O without altering the validated client behavior."""

    def __init__(self, client: Any, counters: dict[str, int]) -> None:
        self._client = client
        self._counters = counters
        self.indices = client.indices
        self.cluster = client.cluster
        self.nodes = client.nodes

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        self._counters["opensearch_reads"] += 1
        return await self._client.get(*args, **kwargs)

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        self._counters["opensearch_reads"] += 1
        return await self._client.search(*args, **kwargs)

    async def count(self, *args: Any, **kwargs: Any) -> Any:
        self._counters["opensearch_reads"] += 1
        return await self._client.count(*args, **kwargs)

    async def update_by_query(self, *args: Any, **kwargs: Any) -> Any:
        self._counters["opensearch_writes"] += 1
        return await self._client.update_by_query(*args, **kwargs)


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cgroup_memory() -> dict[str, Any]:
    events: dict[str, int] = {}
    try:
        for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines():
            key, value = line.split(maxsplit=1)
            events[key] = int(value)
    except (OSError, ValueError):
        pass
    return {
        "current_bytes": _read_int("/sys/fs/cgroup/memory.current"),
        "peak_bytes": _read_int("/sys/fs/cgroup/memory.peak"),
        "events": events,
    }


async def _resource_sample(client: Any) -> dict[str, Any]:
    health = await client.cluster.health()
    allocation = await client.cat.allocation(format="json", bytes="b")
    node_stats = await client.nodes.stats(metric="jvm,process,os,fs")
    opensearch_nodes = []
    for node_id, value in node_stats.get("nodes", {}).items():
        process = value.get("process", {})
        jvm = value.get("jvm", {})
        opensearch_nodes.append(
            {
                "node_id": node_id,
                "name": value.get("name"),
                "cpu_percent": process.get("cpu", {}).get("percent"),
                "virtual_memory_bytes": process.get("mem", {}).get("total_virtual_in_bytes"),
                "heap_used_bytes": jvm.get("mem", {}).get("heap_used_in_bytes"),
                "heap_max_bytes": jvm.get("mem", {}).get("heap_max_in_bytes"),
                "load_average": value.get("os", {}).get("cpu", {}).get("load_average"),
            }
        )
    try:
        load_average = Path("/proc/loadavg").read_text().split()[:3]
    except OSError:
        load_average = []
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "opensearch_health": str(health.get("status") or "unknown"),
        "unassigned_shards": int(health.get("unassigned_shards") or 0),
        "allocation": allocation,
        "opensearch_nodes": opensearch_nodes,
        "backend_cgroup_memory": _cgroup_memory(),
        "backend_node_load_average": load_average,
    }


def _guard_sample(sample: dict[str, Any], *, initial_oom_kill: int) -> None:
    if sample["opensearch_health"] != "green" or sample["unassigned_shards"] != 0:
        raise RuntimeError("fail_closed_guard:opensearch_not_green")
    memory = sample["backend_cgroup_memory"]
    if int(memory.get("current_bytes") or 0) > 3 * 1024**3:
        raise RuntimeError("fail_closed_guard:backend_memory_above_3GiB")
    oom_kill = int((memory.get("events") or {}).get("oom_kill") or 0)
    if oom_kill > initial_oom_kill:
        raise RuntimeError("fail_closed_guard:new_backend_oom_kill")
    for allocation in sample.get("allocation", []):
        if int(allocation.get("disk.percent") or 0) >= 80:
            raise RuntimeError("fail_closed_guard:opensearch_disk_above_80_percent")
    for node in sample.get("opensearch_nodes", []):
        if int(node.get("heap_used_bytes") or 0) > 6 * 1024**3:
            raise RuntimeError("fail_closed_guard:opensearch_heap_above_6GiB")


async def _select_records(
    client: Any,
    *,
    index_name: str,
    manifest: dict[str, Any],
    local_archive_root: str,
) -> tuple[list[IndexedDocumentRecord], dict[str, int]]:
    grouped: dict[str, list[tuple[IndexedDocumentRecord, Any]]] = defaultdict(list)
    async for record in scan_indexed_documents(client, index_name=index_name, batch_size=500):
        decision = await asyncio.to_thread(
            map_archived_original,
            record,
            local_archive_root=local_archive_root,
            manifest=manifest,
            verify_local_hash=False,
        )
        grouped[record.document_id].append((record, decision))
    selected: list[IndexedDocumentRecord] = []
    eligibility: Counter[str] = Counter()
    for document_id in sorted(grouped):
        candidates = grouped[document_id]
        candidates.sort(
            key=lambda value: (
                value[1].status is not MetadataBackfillStatus.SUCCESS,
                not bool(value[0].current_metadata_facts_sha256),
                value[0].source_entity_id,
                value[0].source_url,
                value[0].storage_id,
            )
        )
        record, decision = candidates[0]
        selected.append(record)
        eligibility[decision.status.value] += 1
    return selected, dict(sorted(eligibility.items()))


def _validate_existing_batch(path: Path, records: list[IndexedDocumentRecord]) -> None:
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    observed = {
        storage_id: _record_identity(IndexedDocumentRecord.model_validate(item["record"]))
        for storage_id, item in payload.get("items", {}).items()
    }
    expected = {record.storage_id: _record_identity(record) for record in records}
    if observed != expected:
        raise RuntimeError(f"checkpoint cohort mismatch: {path.name}")


def _prepare_transient_retries(checkpoint: CanaryCheckpoint, max_retries: int) -> int:
    prepared = 0
    for storage_id, item in list(checkpoint.data.get("items", {}).items()):
        state = str(item.get("state") or "")
        reason = str(item.get("reason") or "")
        attempts = int(item.get("transient_retries") or 0)
        if attempts >= max_retries:
            continue
        if state == CanaryStatus.FAILED.value and reason.startswith(TRANSIENT_FAILURE_PREFIXES):
            resume_state = (
                CanaryStatus.EXTRACTED
                if reason.startswith("opensearch_write_failed:") and item.get("profile")
                else CanaryStatus.PENDING
            )
            checkpoint.transition(
                storage_id,
                resume_state,
                reason=f"bounded_retry:{reason}",
                transient_retries=attempts + 1,
            )
            prepared += 1
    return prepared


def _normalize_unsupported(checkpoint: CanaryCheckpoint) -> int:
    changed = 0
    for storage_id, item in list(checkpoint.data.get("items", {}).items()):
        if item.get("state") == CanaryStatus.SKIPPED.value:
            checkpoint.transition(
                storage_id,
                CanaryStatus.FAILED,
                reason=f"unsupported_format:{item.get('reason') or 'unspecified'}",
            )
            changed += 1
    return changed


async def run(args: argparse.Namespace) -> int:
    started_monotonic = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    root = Path(args.checkpoint_directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    manifest = load_archive_manifest(args.archive_manifest)
    raw_client = clients.create_index_admin_opensearch_client()
    if raw_client is None:
        raise RuntimeError("an index-admin OpenSearch client is required")
    counter_path = root / "io-counters.json"
    counters = {"opensearch_reads": 0, "opensearch_writes": 0}
    if counter_path.exists():
        counters.update(json.loads(counter_path.read_text(encoding="utf-8")))
    client = CountingClient(raw_client, counters)
    try:
        preflight = await _resource_sample(raw_client)
        initial_oom_kill = int(
            (preflight.get("backend_cgroup_memory", {}).get("events") or {}).get("oom_kill") or 0
        )
        _guard_sample(preflight, initial_oom_kill=initial_oom_kill)
        records, eligibility = await _select_records(
            client,
            index_name=args.index,
            manifest=manifest,
            local_archive_root=args.local_archive_root,
        )
        eligible = int(eligibility.get(MetadataBackfillStatus.SUCCESS.value, 0))
        unavailable = int(eligibility.get(MetadataBackfillStatus.NO_ARCHIVE_SOURCE.value, 0))
        ambiguous = int(eligibility.get(MetadataBackfillStatus.AMBIGUOUS_SOURCE.value, 0))
        existing_profiles = sum(bool(record.current_metadata_facts_sha256) for record in records)
        if len(records) != args.expected_documents:
            raise RuntimeError(
                f"document gate mismatch:{len(records)}!={args.expected_documents}"
            )
        if eligible != args.expected_eligible:
            raise RuntimeError(f"eligibility gate mismatch:{eligible}!={args.expected_eligible}")
        if unavailable != args.expected_unavailable or ambiguous != 0:
            raise RuntimeError(
                "unavailable/ambiguous gate mismatch:"
                f"{unavailable}/{ambiguous}!={args.expected_unavailable}/0"
            )
        identities = [_record_identity(record) for record in records]
        cohort_digest = _canonical_sha256(identities)
        run_identity = {
            "schema": "openrag.document-metadata-full-backfill-run",
            "version": 1,
            "index": args.index,
            "documents": len(records),
            "eligible": eligible,
            "archive_unavailable": unavailable,
            "ambiguous": ambiguous,
            "existing_profiles": existing_profiles,
            "batch_size": args.batch_size,
            "cohort_sha256": cohort_digest,
        }
        identity_path = root / "run-identity.json"
        if identity_path.exists():
            observed_identity = json.loads(identity_path.read_text(encoding="utf-8"))
            if observed_identity != run_identity:
                raise RuntimeError("run identity differs from durable checkpoint")
        else:
            _atomic_private_json(identity_path, run_identity)
        batch_count = (len(records) + args.batch_size - 1) // args.batch_size
        output_path = Path(args.output)
        progress_path = Path(args.progress_log)
        samples_path = Path(args.resource_log)
        _append_private_jsonl(
            progress_path,
            {
                "timestamp": started_at,
                "event": "started",
                **run_identity,
                "cluster_health": preflight["opensearch_health"],
                "checkpoint_id": cohort_digest,
            },
        )
        next_progress = {
            value for value in (1000, 5000, 10000, 25000, len(records)) if value <= len(records)
        }
        previous_processed = _aggregate(root, batch_count)["processed"]
        for batch_index in range(batch_count):
            batch = records[
                batch_index * args.batch_size : (batch_index + 1) * args.batch_size
            ]
            checkpoint_path = _batch_checkpoint_path(root, batch_index)
            _validate_existing_batch(checkpoint_path, batch)
            checkpoint = CanaryCheckpoint(checkpoint_path)
            checkpoint.initialize(
                index_name=args.index,
                records=batch,
                run_id=f"{cohort_digest[:16]}-{batch_index:05d}",
            )
            resolver = ArchivedOriginalResolver(
                openarchiver_base_url=args.openarchiver_base_url,
                openarchiver_api_key_file=args.openarchiver_api_key_file,
                temporary_directory=args.temporary_directory,
                timeout_seconds=args.archive_timeout,
                download_attempts=args.archive_retries,
            )
            canary = DocumentMetadataCanary(
                client,
                index_name=args.index,
                checkpoint=checkpoint,
                manifest=manifest,
                local_archive_root=args.local_archive_root,
                resolver=resolver,
            )
            for _attempt in range(args.transient_retries + 1):
                await canary.run()
                _normalize_unsupported(checkpoint)
                nonterminal = [
                    item
                    for item in checkpoint.data["items"].values()
                    if str(item.get("state")) not in EXPECTED_TERMINAL_STATES
                ]
                retries = _prepare_transient_retries(checkpoint, args.transient_retries)
                if not nonterminal and retries == 0:
                    break
            remaining_nonterminal = [
                item
                for item in checkpoint.data["items"].values()
                if str(item.get("state")) not in EXPECTED_TERMINAL_STATES
            ]
            if remaining_nonterminal:
                raise RuntimeError(
                    f"batch {batch_index} retained {len(remaining_nonterminal)} nonterminal items"
                )
            aggregate = _aggregate(root, batch_count)
            _atomic_private_json(counter_path, counters)
            sample = await _resource_sample(raw_client)
            _guard_sample(sample, initial_oom_kill=initial_oom_kill)
            sample["processed"] = aggregate["processed"]
            _append_private_jsonl(samples_path, sample)
            processed = int(aggregate["processed"])
            crossed = sorted(
                value for value in next_progress if previous_processed < value <= processed
            )
            if crossed:
                elapsed = time.monotonic() - started_monotonic
                progress = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": "progress" if processed < len(records) else "completion",
                    "processed": processed,
                    "eligible": eligible,
                    "states": aggregate["states"],
                    "bytes_read": aggregate["bytes_read"],
                    "opensearch_reads": counters["opensearch_reads"],
                    "opensearch_writes": aggregate["opensearch_writes"],
                    "errors": sum(
                        int(aggregate["states"].get(state, 0))
                        for state in ("FAILED", "DLS_BLOCKED")
                    ),
                    "remaining": len(records) - processed,
                    "elapsed_seconds": elapsed,
                    "docs_per_second": processed / elapsed if elapsed else 0,
                    "cluster_health": sample["opensearch_health"],
                    "checkpoint_id": cohort_digest,
                    "checkpoint_digest": _canonical_sha256(
                        {"processed": processed, "states": aggregate["states"]}
                    ),
                    "milestones": crossed,
                }
                _append_private_jsonl(progress_path, progress)
                print(json.dumps(progress, sort_keys=True), flush=True)
                next_progress.difference_update(crossed)
            previous_processed = processed
        aggregate = _aggregate(root, batch_count)
        final_sample = await _resource_sample(raw_client)
        _guard_sample(final_sample, initial_oom_kill=initial_oom_kill)
        finished_at = datetime.now(UTC).isoformat()
        elapsed = time.monotonic() - started_monotonic
        terminal_total = sum(int(value) for value in aggregate["states"].values())
        if terminal_total != len(records):
            raise RuntimeError(f"terminal accounting mismatch:{terminal_total}!={len(records)}")
        result = {
            "schema": "openrag.document-metadata-full-backfill-summary",
            "version": 1,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed,
            **run_identity,
            "processed": aggregate["processed"],
            "states": aggregate["states"],
            "bytes_read": aggregate["bytes_read"],
            "opensearch_reads": counters["opensearch_reads"],
            "opensearch_writes": aggregate["opensearch_writes"],
            "formats": aggregate["formats"],
            "failure_reasons": aggregate["failure_reasons"],
            "timings": _timing_summary(aggregate["timing_values"]),
            "docs_per_second": aggregate["processed"] / elapsed if elapsed else 0,
            "megabytes_per_second": aggregate["bytes_read"] / 1_000_000 / elapsed
            if elapsed
            else 0,
            "preflight": preflight,
            "final_resource_sample": final_sample,
            "checkpoint_directory": str(root),
            "progress_log": str(progress_path),
            "resource_log": str(samples_path),
        }
        _atomic_private_json(output_path, result)
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        return 0
    finally:
        await raw_client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute the explicitly approved full historical metadata backfill"
    )
    parser.add_argument("--index", default=get_index_name())
    parser.add_argument("--archive-manifest", required=True)
    parser.add_argument("--local-archive-root", default=get_indexed_documents_path())
    parser.add_argument("--openarchiver-base-url", required=True)
    parser.add_argument("--openarchiver-api-key-file", required=True)
    parser.add_argument("--temporary-directory", required=True)
    parser.add_argument("--checkpoint-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--progress-log", required=True)
    parser.add_argument("--resource-log", required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--archive-timeout", type=float, default=120)
    parser.add_argument("--archive-retries", type=int, default=3)
    parser.add_argument("--transient-retries", type=int, default=2)
    parser.add_argument("--expected-documents", type=int, default=47400)
    parser.add_argument("--expected-eligible", type=int, default=47362)
    parser.add_argument("--expected-unavailable", type=int, default=38)
    parser.add_argument("--confirm-full-backfill", default="")
    args = parser.parse_args()
    if args.confirm_full_backfill != WRITE_CONFIRMATION:
        parser.error(f"writes require --confirm-full-backfill {WRITE_CONFIRMATION}")
    if args.batch_size < 50 or args.batch_size > 150:
        parser.error("--batch-size must be between 50 and 150")
    if args.archive_retries < 1 or args.archive_retries > 5:
        parser.error("--archive-retries must be between 1 and 5")
    if args.transient_retries < 0 or args.transient_retries > 3:
        parser.error("--transient-retries must be between 0 and 3")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
