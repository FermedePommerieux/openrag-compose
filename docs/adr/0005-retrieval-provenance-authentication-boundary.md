# ADR 0005 — Retrieval provenance and authentication boundary

## Context

Chat crosses Langflow before it returns document citations, and browser cookie
authentication alone does not survive that internal HTTP hop.

## Decision

The identity resolver accepts the established OSS cookie or the original
end-user Bearer JWT. Search preserves chunk and source provenance and always
uses a user-scoped OpenSearch client with refreshed DLS principals. The API,
chat SSE source events, and both SDKs preserve optional `document_id`,
`chunk_id`, `connector_file_id`, `chunk_index`, `chunking_strategy`, and
`source_url` fields without requiring them for legacy documents.

## Reasons

The second hop remains equivalent to a direct browser search without using a
privileged service identity.

## Consequences

Invalid, expired, or absent credentials are rejected. Legacy metadata remains
nullable and readable.

## Rejected alternatives

Forwarding an administrator token, bypassing DLS, or rebuilding provenance in
the agent prompt.
