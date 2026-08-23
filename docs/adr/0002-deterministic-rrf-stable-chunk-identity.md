# ADR 0002 — Deterministic RRF and stable chunk identity

## Context

Equal OpenSearch scores have no guaranteed incidental order, and `_id` is not
a supported sortable field.

## Decision

Persist logical `chunk_id` as a keyword with doc values and use it as the
secondary lane sort and final RRF tie-break. Temporary storage generation ids
never change the logical identity.

## Reasons

This makes equivalent indexed chunks reproducible across re-indexes.

## Consequences

Legacy chunks without `chunk_id` sort last for equal scores until re-indexed;
full multi-shard reproducibility still requires integration verification.

## Rejected alternatives

Sorting `_id`, Python object identity, or a random process-local hash.
