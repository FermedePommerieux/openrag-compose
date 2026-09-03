# Structured metadata Agent search v1

## Boundary

`document_search_with_metadata` is a Langflow tool backed by
`POST /search/metadata-agent`. Its public input is
`openrag.metadata-agent-search v1`: `free_text`, one to eight strict filters,
and a result limit from 1 to 20. Each filter has a versioned field, one of
`EQUAL`, `IN`, `EXISTS`, `NOT_EXISTS`, or `NOT_EQUAL`, a scalar/list value when
required, and an explicit calendar basis for temporal fields. Unknown fields,
operators, extra properties, raw OpenSearch/Lucene JSON, scripts, and nested
query DSL are rejected by Pydantic before execution. `IN` is limited to 16
values and each scalar to 256 characters.

The backend compiles this schema only to existing `openrag.metadata-filter v1`
clauses. `NOT_EQUAL` becomes the already-proven negated `EQUAL` clause, so
UNKNOWN remains distinct from FALSE and negation remains fail-closed. The
existing DLS-scoped side-index resolver produces eligible visible occurrence
IDs, and the unchanged lexical/dense lanes are restricted to the same IDs.
RRF selects filtered seeds, after which the existing `scope_exhaustive`
documentary PROV-O closure and evidence projection run unchanged.
`openrag.scope-coverage v1` therefore still certifies only the accessible
strong closure discovered from those seeds. No association neighborhood or
metadata ranking is involved.

## Natural-language planning

The planning mode is `DETERMINISTIC_ONLY`. Before Langflow receives a chat
request, the backend recognizes a deliberately narrow French/English grammar
for technical format, production/modification calendar, OpenArchiver source,
and exact creator observations. Natural calendar wording defaults to
`SOURCE_LOCAL`; UTC is used only when explicitly requested. The deterministic
plan and its SHA-256 are request-scoped Langflow globals.

The Agent may choose the metadata tool, but its proposed arguments must match
the deterministic plan exactly. A mismatch returns `INVALID` without HTTP
search. The normal `search_documents` tool refuses a request for which a valid
metadata plan exists, preventing a silent broader fallback. `AMBIGUOUS` and
`UNSUPPORTED` plans block both retrieval paths and return bounded diagnostics.
Queries with no metadata intent continue through normal q1 retrieval.

Explicit filters supplied to the planner are preserved exactly and replace
only inferred predicates on the same fields. There is no fuzzy person
resolution. “Invoice/facture” remains semantic free text, not a declared
document type. OpenArchiver becomes `source_system=openarchiver` only in a
source/document context. Attachment relationships are never inferred.

The parser explicitly blocks archive/ingestion calendars, missing calendar
years, natural-language ranges, cross-role temporal disjunctions, disjunctive
formats, and implicit parent-source identity. Pure metadata requests retain
the neutral word “documents” as q1 free text because the validated production
contract intentionally rejects filter-only partial retrieval.

## Observation wording

User-facing explanations must describe observation semantics. For example:

> J’ai limité la recherche aux PDF dont au moins une observation valide de
> date de production en calendrier source-local indique mars 2024.

They must not claim that the filter proves an unconditional document fact.

## Evaluation

`corpus-v1.json` contains 45 generic French/English cases spanning dates,
formats, sources, creators, combinations, unsupported and ambiguous intent,
no-metadata requests, and false-positive traps. `metadata_planner.py` measures
exact structured parsing, false-positive/false-negative filter rates,
unsupported and ambiguity detection, free-text preservation, latency, LLM
calls, and model-token overhead. `results-v1.json` is the captured Phase 1 run.
