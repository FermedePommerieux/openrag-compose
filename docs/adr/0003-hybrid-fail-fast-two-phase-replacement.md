# ADR 0003 — Hybrid fail-fast and two-phase replacement

## Context

Hybrid chunking can fail after a replacement has started. Deleting the old
chunks first loses a previously usable document.

## Decision

An explicit hybrid request fails rather than falling back to character chunks.
New chunks are indexed as a temporary generation, then promoted only after a
complete success; old chunks are deleted afterwards.

## Reasons

Users retain the last valid source during parser, chunker, embedding, or index
failures.

## Consequences

Temporary generations require cleanup on failure and deduplication includes
chunking strategy and relevant parameters.

## Rejected alternatives

Pre-delete/upsert replacement and silent character fallback.
