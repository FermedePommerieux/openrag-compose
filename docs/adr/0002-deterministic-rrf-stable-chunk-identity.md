# ADR 0002 — Deterministic RRF and stable chunk identity

## Context

Equal OpenSearch scores have no guaranteed incidental order, and `_id` is not
a supported sortable field.

## Decision

Persist logical `chunk_id` as a keyword with doc values and use it as the
secondary lane sort and final RRF tie-break. Temporary storage generation ids
never change the logical identity.

RRF is unavailable when the actual index mapping is missing `chunk_id`, maps
it as `text`, or disables doc values. OpenRAG validates this precondition at
query time and fails with a reindexing instruction rather than executing a
sort known to be invalid.

## Reasons

This makes equivalent indexed chunks reproducible across re-indexes.

## Consequences

Legacy chunks without `chunk_id` sort last for equal scores until re-indexed;
full multi-shard reproducibility still requires integration verification.
That is compatible with RRF because the mapping remains sortable; a legacy
*document value* is not an incompatible mapping.

## Rejected alternatives

Sorting `_id`, Python object identity, or a random process-local hash.
