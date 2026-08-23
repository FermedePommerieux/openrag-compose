# ADR 0004 — Langflow flow migration policy

## Context

Persisted flows survive application upgrades, while the historical agent used
the old OpenSearch retrieval component.

## Decision

Startup migrates only the recognized locked lifecycle graph after creating a
backup. The migration replaces the retrieval node and tool edge, is idempotent,
and refuses custom or unrecognized flows.

## Reasons

Known defaults can be upgraded safely without overwriting administrator work.

## Consequences

An explicit migration error requires operator review. Rollback restores the
saved flow JSON through Langflow.

## Rejected alternatives

Blindly overwriting every flow or silently leaving known legacy flows active.
