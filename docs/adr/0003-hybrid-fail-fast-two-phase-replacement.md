# ADR 0003 — Hybrid fail-fast and two-phase replacement

## Context

Hybrid chunking can fail after a replacement has started. Deleting the old
chunks first loses a previously usable document.

## Decision

An explicit hybrid request fails rather than falling back to character chunks.
New chunks are indexed as a temporary generation, then promoted only after a
complete success; old chunks are deleted afterwards.

Every snapshot, selection, promotion, delete, rollback and cleanup is scoped
by the logical document identity **and** owner/shared scope. A content hash is
not globally unique: two owners can upload identical bytes. Administrative
writes therefore never delete by `document_id` alone.

## Reasons

Users retain the last valid source during parser, chunker, embedding, or index
failures.

## Consequences

Temporary generations require cleanup on failure and deduplication includes
chunking strategy and relevant parameters. If a per-id delete partly succeeds,
the prior snapshot is bulk-restored and every bulk item is checked. A partial
or unverifiable restore is a fatal incident, never reported as a successful
rollback.

## Rejected alternatives

Pre-delete/upsert replacement and silent character fallback.
