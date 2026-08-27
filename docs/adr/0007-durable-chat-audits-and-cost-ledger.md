# ADR 0007: Durable chat audits and a per-request cost ledger

Status: accepted

## Context

An exhaustive archive audit can outlive a browser, reverse-proxy or framework
stream timeout. Previously the backend response generator owned the Langflow
stream. When the subscriber disappeared, the tool-side search could continue
and consume provider tokens, but its final answer and usage event had nowhere
to go. Progress was also process-local and was not a recoverable job result.

The truth contract additionally requires cost transparency. A Langflow answer
usage event covers the orchestration response, but an exhaustive audit makes
separate reasoning and query-embedding calls. Reporting only the final call is
materially misleading.

## Decision

- Explicit exhaustive chat requests create a UUID audit job owned by the
  authenticated storage user.
- A detached backend task, not the HTTP response, consumes the Langflow stream.
- OpenRAG's persistent backend database stores the sanitized progress
  certificate, terminal answer, response identifier, accumulated usage and
  error state. It is SQLite by default and can be configured as PostgreSQL. It
  never stores JWTs, provider credentials, raw prompts or additional document
  text.
- The initial stream emits `openrag.audit.created`. If that stream ends before
  the terminal usage event, the frontend polls `GET /chat/audits/{audit_id}`
  and continues showing progress rather than starting duplicate work.
- Ownership failures and unknown identifiers both return 404.
- Each OpenAI response is metered at its execution boundary. The ledger sums
  agent generation, archive reasoning workers and query embeddings. Cached and
  reasoning tokens remain visible.
- Cost is computed per provider call from a dated public price table. This is
  necessary because the GPT-5.5/5.6 long-context multiplier applies per call,
  not to an aggregate. Unknown model prices produce `cost_complete=false`
  instead of an invented estimate.

## Consequences and limits

Loss of the browser or its SSE proxy no longer loses or cancels the audit. A
completed result remains recoverable after a backend restart. A backend restart
during active inference cannot replay provider credentials safely, so such a
job may still require an explicit retry; durable distributed execution is a
separate queue/worker concern.

Displayed USD cost is an audit-friendly estimate based on public standard API
rates, not an invoice. Regional processing uplifts, negotiated pricing, cache
writes or provider-specific surcharges may differ. Token counts themselves are
the authoritative usage values returned by the provider.
