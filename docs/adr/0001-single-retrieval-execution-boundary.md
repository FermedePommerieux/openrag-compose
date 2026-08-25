# ADR 0001 — Single retrieval execution boundary

## Context

Langflow previously contained OpenSearch query logic while API and SDK paths
also evolved retrieval behaviour.

## Decision

Langflow uses a thin `search_documents` tool; the backend owns query building,
retrieval strategy, filters, provenance, and ACL-scoped OpenSearch access.

## Reasons

One implementation prevents BM25/KNN/RRF policy and security drift.

## Consequences

Flows must not add a second retrieval engine. Backend retrieval changes affect
chat, API, and SDK uniformly.

## Rejected alternatives

Keeping a feature-complete OpenSearch component in each Langflow flow.
