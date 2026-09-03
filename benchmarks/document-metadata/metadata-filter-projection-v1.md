# `openrag.metadata-filter-projection v1`

Status: internal, implemented and canary-validated; not connected to public search.

## Contract and identity

The projection is derived from `openrag.document-metadata v1` and complete,
explicit indexed source context. It never replaces or edits the raw profile.
The projection carries:

- `contract = openrag.metadata-filter-projection`
- `projection_version = 1`
- `source_metadata_facts_sha256`
- `source_context_sha256`
- `projection_sha256`

`source_metadata_facts_sha256` detects a stale projection after raw metadata
changes. `source_context_sha256` covers the non-profile source facts used by
the projection. `projection_sha256` hashes the entire canonical derived
payload. A missing side row, a source digest mismatch, or a projection digest
mismatch is fail-closed and must not be interpreted as a negative fact.

All arrays are sorted, deduplicated, and multi-valued. There is no
`created_at`, `modified_at`, preferred timestamp, preferred creator, or silent
conflict resolution.

## Indexed field inventory

| Group | Fields | Mapping |
|---|---|---|
| Identity | `contract`, `projection_version`, source/projection digests | `keyword` / `integer` |
| Production local | `production_day_local[]`, `production_month_local[]`, `production_year_local[]` | `date(strict_date)`, `keyword`, `keyword` |
| Production UTC | `production_day_utc[]`, `production_month_utc[]`, `production_year_utc[]` | `date(strict_date)`, `keyword`, `keyword` |
| Modification local | `modification_day_local[]`, `modification_month_local[]`, `modification_year_local[]` | `date(strict_date)`, `keyword`, `keyword` |
| Modification UTC | `modification_day_utc[]`, `modification_month_utc[]`, `modification_year_utc[]` | `date(strict_date)`, `keyword`, `keyword` |
| Presence/uncertainty | `has_production_observation`, `has_valid_production_observation`, `has_modification_observation`, `has_valid_modification_observation`, `has_timezone_unknown`, `has_invalid_timestamp`, `has_temporal_conflict` | `boolean` |
| Temporal evidence | `production_observation_sources[]`, `modification_observation_sources[]`, `temporal_observations[]` | `keyword`, `nested` |
| Type | `mime_types[]`, `format_families[]`, `extensions[]`, `explicit_document_types[]` | `keyword` |
| Source | `source_systems[]`, `source_entity_types[]`, `source_entity_families[]`, `source_connectors[]`, `parent_collection_ids_safe[]` | `keyword` |
| Actors/apps | `creator_normalized[]`, `last_modifier_normalized[]`, `producer_normalized[]`, `creator_application_normalized[]` | `keyword` |
| File/binary | `filename_basename_normalized[]`, `binary_sha256[]` | `keyword` |
| Conflict | `has_metadata_conflict`, `conflict_types[]` | `boolean`, `keyword` |
| Returned evidence | `value_observations[]` | stored `object`, indexing disabled |

Calendar days use OpenSearch `date`; months and years remain exact keywords
because they are calendar periods rather than instants. Lexicographic range
ordering is valid for canonical `YYYY-MM` and `YYYY` values.

`temporal_observations` preserves role, canonical source, status, timezone
status, and local/UTC periods together. This is required for an explicit-source
filter without cross-matching one observation's source to another
observation's value.

Canonical temporal sources are `pdf_info`, `pdf_xmp`, `ooxml_core`,
`eml_header`, `exif`, `xmp`, `archive`, `filesystem`, `ingestion`, and
`other_format_native`. An unregistered explicit-source query fails closed.

`explicit_document_types` is populated only from an explicit metadata
observation. `source_entity_families` is also populated only from an explicit
family observation: `source_entity_type` is never reinterpreted as
`SAME_SOURCE_ENTITY_FAMILY`.

Creator/producer values use only NFKC + whitespace collapse + casefold. There
is no fuzzy matching or identity resolution. Parent collection locators are
projected as namespace-bound SHA-256 equality keys and never exposed raw.

## Three-valued query semantics

The compiler constructs disjoint TRUE and FALSE predicates for each leaf; all
documents in neither set are UNKNOWN. Only the TRUE predicate is sent as an
eligible OpenSearch filter. A projection-existence guard excludes missing
projections from every positive result.

For a positive comparison, one matching usable observation is TRUE, usable
observations with no match are FALSE, and no usable temporal observation is
UNKNOWN. For its negation, TRUE and FALSE swap while UNKNOWN remains UNKNOWN.
An invalid or timezone-indeterminate observation cannot produce a UTC value.

Strong-Kleene composition is used recursively:

| AND | TRUE | FALSE | UNKNOWN |
|---|---|---|---|
| TRUE | TRUE | FALSE | UNKNOWN |
| FALSE | FALSE | FALSE | FALSE |
| UNKNOWN | UNKNOWN | FALSE | UNKNOWN |

| OR | TRUE | FALSE | UNKNOWN |
|---|---|---|---|
| TRUE | TRUE | TRUE | TRUE |
| FALSE | TRUE | FALSE | UNKNOWN |
| UNKNOWN | TRUE | UNKNOWN | UNKNOWN |

| NOT | Result |
|---|---|
| TRUE | FALSE |
| FALSE | TRUE |
| UNKNOWN | UNKNOWN |

## Storage decision

| Option | Query efficiency | DLS | Update/mapping risk | Join | Isolation |
|---|---|---|---|---|---|
| Representative chunk | Metadata lookup is fast, but retrieval still needs occurrence IDs; duplicating onto all chunks would multiply writes | Inherited | Rewrites vector-bearing Lucene documents; may perturb HNSW and adds mapping to the retrieval index | Required unless duplicated | Low |
| Side document in retrieval index | Fast metadata lookup | ACL must be copied | Adds non-content documents to retrieval index and can change BM25 corpus statistics/IDF unless every lane excludes them | Required | Medium |
| Dedicated side index | Fast exact filters/counts/facets | Exact ACL envelope copied; existing `documents*` DLS role applies | No BM25/HNSW rewrite; independent strict mapping and rollback | Required two-phase restriction | High |

Recommendation: a dedicated side index, one row per source occurrence. The
side row copies only `owner`, `allowed_users`, `allowed_groups`, and
`allowed_principals`; it has the same DLS boundary as the source document.
Failure or rollback deletes/rebuilds projection rows only.

The future integration must query the side index with the user's DLS-scoped
OpenSearch client, paginate eligible occurrence IDs, and restrict lexical and
dense retrieval to that eligible set. Large/broad eligible sets require an
explicit pagination/transfer design in the next chantier. ANN remains
`APPROXIMATE_MEMBERSHIP` within the structurally eligible set.

Metadata filtering affects discovery candidates only. It adds zero PROV-O
edges, performs zero scope expansion, and does not change
`openrag.scope-coverage v1`. Projection generation and filtering require zero
LLM calls.

## Canary and full-projection gate

The canary index namespace is restricted to
`documents-metadata-filter-projection-canary-*`, cohort size is hard-bounded to
100–500, and an existing index is never reused. An idempotent apply compares
`projection_sha256` before writing. Rollback accepts only the exact canary
namespace and removes the side index; it has no source-index mutation method.

The validated evidence is stored in
`metadata-filter-projection-canary.json`. It is not authorization to project
the full corpus or connect filters to `/api/search`.
