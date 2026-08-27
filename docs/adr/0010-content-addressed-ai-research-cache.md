# ADR 0010 — Content-addressed AI research cache

## Status

Accepted for Retrieval v2 version 16.

## Context

An archive audit can repeat the same structured reasoning operation: grounded
query expansion, independent evidence extraction, reduction, or claim
verification. Re-sending an identical evidence request wastes tokens and can
produce needless output variation. Conversely, caching a final conversational
answer or reusing a merely similar query would be unsafe: permissions, source
content, the requested subject, or the validation schema may have changed.

GPT-5.6 reasoning models also require the OpenAI Responses API when function
tools and non-null reasoning effort are combined. Sending that contract to
`/v1/chat/completions` fails before retrieval can run.

## Decision

1. The Langflow Agent selects `use_responses_api=True` before the first model
   request when the explicit provider is OpenAI and the selected model name
   begins with `gpt-5.6`. Other providers and models retain their existing
   transport. We do not silence reasoning with `reasoning_effort=none`.
   The automatic provider-health banner is inference-free even when a prior
   chat error asks for a completion test; it validates credentials through the
   Models endpoint. Only an explicitly named provider or onboarding action may
   run one paid compatibility probe, and GPT-5.6 probes use Responses too.
2. OpenRAG caches only Pydantic-validated structured audit responses. Final
   prose answers are never cached.
3. A cache key is SHA-256 over a canonical contract containing:

   - a cache protocol version and namespace;
   - a SHA-256 user/authorization scope;
   - exact model and structured-output name;
   - the complete JSON Schema;
   - the complete system and evidence prompt.

4. Prompts and raw request bodies are not stored. The validated JSON response,
   original usage summary, timestamps and hit count are stored in PostgreSQL or
   SQLite through the normal OpenRAG database.
5. Entries are retained without expiration by default because a verified
   documentary extraction remains useful research work. Set
   `OPENRAG_AI_RESPONSE_CACHE_TTL_DAYS` to a positive integer only when a
   bounded retention policy is required; zero also means unlimited. Cache
   storage can be disabled independently with
   `OPENRAG_AI_RESPONSE_CACHE_ENABLED=false`.
6. Cache read/write failure is fail-open: the provider request proceeds and
   retrieval correctness does not depend on cache availability.
7. Application-cache savings are reported separately from billed usage as
   avoided calls, tokens and estimated cost. A cache hit adds zero actual
   provider tokens and zero actual cost.

## Why this remains truthful

The current DLS-scoped OpenSearch search and full-document read still run
before a reasoning request is constructed. A source edit changes evidence text
and therefore the cache key. A schema or prompt-policy change also changes the
key. A different user produces a different authorization-scope digest. Cached
work can therefore replace only the exact AI operation that would otherwise
have been sent; it cannot make a stale document visible or certify a different
question.

This first cache layer makes repeated searches and repeated agent tool calls
reuse all identical subproblems from the earlier audit. Future cross-query
research memory may add previously proved entities as *additional* OpenSearch
lanes, but it must never replace fresh discovery or exclude candidates on the
basis of semantic similarity.

## Consequences

- Repeating the same audit over unchanged evidence avoids its structured Luna
  calls while Sol still writes a fresh conversational answer.
- A related but materially different question pays for the changed reasoning
  steps; this is intentional because approximate reuse is not a proof.
- Database growth follows the number of distinct verified reasoning contracts,
  without an arbitrary retention deadline. A deployment that explicitly sets
  a positive retention period still ignores expired rows before cleanup.
- Retrieval v2 is bumped to version 16 so the protected production flow can be
  migrated from the exact version-15 graph without overwriting operator edits.
