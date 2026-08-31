# ADR 0007: Generic multi-query documentary discovery

## Status

Experimental — implemented and benchmarked, but not enabled or deployed.

## Context

A literal hybrid query can miss documents that express the same subject with
different vocabulary, administrative wording, actor names, or event-oriented
language. Increasing the ranked chunk budget would mix query-diversity gains
with a larger candidate pool and would not establish whether query expansion
itself improves discovery.

## Decision

The retrieval service offers an opt-in `multi_query_discovery` mode. Its
domain-neutral planner receives only the user's original query and returns at
most three structured variants. The original query is always `q0`; malformed,
duplicate, or empty variants are discarded after deterministic Unicode,
case, punctuation, and whitespace normalization.

Each accepted query runs through the unchanged lexical and dense retrieval
lanes under the same authenticated DLS context and filters. Per-query results
retain their existing lexical+dense RRF ordering. The service then applies a
second, deterministic RRF:

```text
score(chunk) = sum over q of 1 / (rrf_k + per_query_rrf_rank(q, chunk))
```

Stable chunk identity breaks ties. Global per-document diversity and one final
seed budget are applied after fusion. Every result can expose its matched
queries, matched lanes, best rank per query, individual contributions, and
final fusion score.

Only seed discovery changes. `documentary-prov-o` version 1 closure,
relationship semantics, document identity, coverage certificates, and
exhaustive-document reading are unchanged. The Langflow agent still exposes a
single controlled retrieval tool, so one model tool call cannot bypass the
server-side concurrency bound or Agent Guard.

## Failure and safety rules

- A `q0` retrieval failure fails the request.
- Planner failure or a derived-query failure degrades safely to the surviving
  queries and emits an audit warning.
- Query count and concurrency are bounded to four.
- Ground truth, expected documents, component labels, and benchmark outcomes
  are never available to the planner.
- The mode is disabled by default; the default request path and payload remain
  the single-query baseline.

## Validation outcome

The frozen Phase 3 benchmark holds the effective seed budget constant and
compares cumulative `q1` through `q4` runs on an unchanged DLS-visible corpus.
Its decision gate requires a clear seed component recall gain, acceptable
precision and latency, complete PROV-O coverage, and passing validation. A
promising or negative outcome explicitly forbids commit, image build, GitOps,
and deployment for this experiment.
