# ADR 0004 — Langflow flow migration policy

## Context

Persisted flows survive application upgrades, while the historical agent used
the old OpenSearch retrieval component.

## Decision

Startup migrates only the exact recognized locked lifecycle graph after
creating a backup. The signature is versioned in source and includes the
expected OpenRAG flow ID, lock state, title, description, and canonical SHA-256
fingerprint of the full `data` graph from lifecycle baseline `156f3664`.

For Langflow 0.11.2, OpenRAG uses the supported flow API sequence:

1. save the original JSON backup;
2. `PATCH /api/v1/flows/{id}` with `locked: false`;
3. `PUT` the bundled Retrieval v2 system graph;
4. `GET` and verify the exact versioned graph and wiring;
5. `PATCH` with `locked: true`; and
6. `GET` again to verify the final locked state.

The second execution recognises the verified version marker and changes
nothing. Any custom, unlocked, altered, or ambiguously migrated graph remains
intact and receives an explicit manual-migration diagnostic.

## Reasons

The native Langflow lock primitive retains system-flow protection outside the
short, verified maintenance window. The full replacement graph prevents stale
legacy embeddings or tool edges from surviving a partial node splice.

## Consequences

An explicit migration error requires operator review. Rollback restores the
saved flow JSON through Langflow and re-locks it.

## Rejected alternatives

Blindly overwriting every flow or silently leaving known legacy flows active.
