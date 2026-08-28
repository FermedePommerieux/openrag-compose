"""Regression tests for CPU-heavy Docling post-processing.

The backend intentionally runs one Uvicorn worker. A synchronous HybridChunker
call on that worker therefore blocks health checks, cancellation requests, and
all other API traffic. These tests pin the thread offload and its cancellation
semantics without depending on a real Docling model.
"""

import asyncio
import threading

import pytest

from models import processors
from utils.document_processing import HybridChunkingError


@pytest.mark.asyncio
async def test_docling_postprocessing_keeps_event_loop_responsive(monkeypatch):
    release = threading.Event()
    worker_thread_ids: list[int] = []
    chunker_thread_ids: list[int] = []
    event_loop_thread_id = threading.get_ident()

    def slow_extract(_full_doc):
        worker_thread_ids.append(threading.get_ident())
        release.wait(timeout=1)
        return {"chunks": []}

    monkeypatch.setattr(processors, "extract_relevant", slow_extract)

    def record_hybrid_thread(*_args, **_kwargs):
        chunker_thread_ids.append(threading.get_ident())
        return []

    monkeypatch.setattr(processors, "chunk_docling_hybrid", record_hybrid_thread)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    heartbeat_task = asyncio.create_task(heartbeat())
    postprocess_task = asyncio.create_task(
        processors._postprocess_docling_output(
            {},
            chunking_strategy="hybrid",
            hybrid_max_tokens=512,
            hybrid_merge_peers=True,
        )
    )
    try:
        for _ in range(100):
            if worker_thread_ids:
                break
            await asyncio.sleep(0.005)
        assert worker_thread_ids
        await asyncio.sleep(0.05)
        assert ticks > 1
        assert worker_thread_ids[0] != event_loop_thread_id
    finally:
        release.set()
        await postprocess_task
        await heartbeat_task

    assert chunker_thread_ids == worker_thread_ids


@pytest.mark.asyncio
async def test_docling_postprocessing_cancellation_does_not_wait_for_worker(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_extract(_full_doc):
        started.set()
        release.wait(timeout=1)
        return {"chunks": []}

    monkeypatch.setattr(processors, "extract_relevant", slow_extract)
    task = asyncio.create_task(
        processors._postprocess_docling_output(
            {},
            chunking_strategy="character",
            hybrid_max_tokens=512,
            hybrid_merge_peers=True,
        )
    )
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_docling_postprocessing_preserves_explicit_hybrid_failure(monkeypatch):
    monkeypatch.setattr(processors, "extract_relevant", lambda _full_doc: {"chunks": []})

    def fail_hybrid(*_args, **_kwargs):
        raise HybridChunkingError("chunking-openai unavailable")

    monkeypatch.setattr(processors, "chunk_docling_hybrid", fail_hybrid)

    with pytest.raises(
        HybridChunkingError,
        match=("requested_chunking_strategy=hybrid effective_chunking_strategy=none"),
    ):
        await processors._postprocess_docling_output(
            {},
            chunking_strategy="hybrid",
            hybrid_max_tokens=512,
            hybrid_merge_peers=True,
        )
