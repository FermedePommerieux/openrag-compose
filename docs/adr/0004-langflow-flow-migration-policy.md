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
nothing. The protected system flow is acceptable only when it is either
`migrated` or `already_migrated`, with the exact Retrieval v2 graph, wiring,
version marker, and final `locked: true` state verified. Any missing flow,
fetch/backup/unlock/update/verification failure, unknown lock state, or graph
that does not match the expected system graph is `system_migration_failed`.
`run_startup` awaits this critical boundary before it schedules background
work, so every such result fails the ASGI lifespan and cannot become Ready.

An operator may explicitly configure a retrieval flow ID other than the
protected system-flow ID. That flow is reported as `custom_preserved`: it is
not overwritten or claimed to use Retrieval v2, and prompt synchronization is
not applied. Startup classifies this configuration before flow creation: it
excludes the custom chat flow from both create/recovery and global settings
reapply, while it may still create missing non-chat system flows. An altered
graph under the protected system-flow ID is ambiguous, therefore it fails
closed rather than being treated as a custom flow.

## Reasons

The native Langflow lock primitive retains system-flow protection outside the
short, verified maintenance window. The full replacement graph prevents stale
legacy embeddings or tool edges from surviving a partial node splice.

The same versioned replacement policy applies to execution-contract repairs.
Retrieval flow version 8 recognizes only the exact version 7 graph and replaces
it with a graph where `OPENRAG_QUERY_FILTER` is loaded as a request-scoped
global variable. Langflow's normalized HTTP alias uses hyphens
(`X-Langflow-Global-Var-OPENRAG-QUERY-FILTER`); the backend and bundled graph
are tested together so exhaustive intent cannot be lost at that boundary.

## Consequences

An explicit system migration error requires operator review and blocks ASGI
startup, even when a prior lock was restored. This avoids serving an old,
partial, or unverified retrieval graph. Rollback restores the saved flow JSON
through Langflow and re-locks it. A `custom_preserved` flow remains the
operator's responsibility and does not receive automatic Retrieval v2 updates.

## Rejected alternatives

Blindly overwriting every flow or silently leaving known legacy flows active.
