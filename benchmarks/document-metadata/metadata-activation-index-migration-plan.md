# Post-backfill metadata activation index plan

Status: `METADATA_FILTER_INDEX_MIGRATION_REQUIRED`

No production index, mapping, alias, corpus document, chunk, embedding, retrieval
default, or PROV-O edge is changed by this plan.

## Assessment

The raw `openrag.document-metadata v1` profile is stored on the representative
occurrence chunk as `document_metadata_profile`, with the OpenSearch object
mapping set to `enabled:false`. It is the complete observational source and
must stay unchanged. It cannot support efficient exact/range filters or
aggregations. A full application-side scan was measured at 216.77 seconds.

Assessment: `DERIVED_FIELDS_REQUIRED`.

The validated target remains a dedicated side index, one row per profiled
source occurrence, using `openrag.metadata-filter-projection v1`. This is not
a rewrite of the BM25/vector corpus. It nevertheless requires an explicitly
approved full projection build before structured filters can be activated.

## Exact migration

1. Reconfirm `/api/settings/runtime-behavior` is `MATCH`, OpenSearch is green,
   5/5 Kubernetes nodes are Ready, backend/frontend are Ready, Langflow is
   3/3, and no pod is OOM/CrashLooping.
2. Create a new immutable generation named
   `documents-metadata-filter-v1-<UTC generation>` with one primary shard,
   zero replicas on the current single-data-node OpenSearch cluster, and
   refresh disabled during bulk construction.
3. Page the 47,454 representative occurrence rows. Generate projections only
   for the expected 47,133 rows with a raw profile. Record the other 321
   occurrences as missing projection/`UNKNOWN`; do not turn them into `FALSE`.
4. Bulk-write bounded batches with deterministic projection document ids,
   durable batch checkpoints, retries, and digest verification. Each row must
   carry the copied ACL envelope and the three freshness digests.
5. Refresh, set a measured steady-state refresh interval, and verify exactly
   47,133 rows, strict mapping, digest integrity, DLS search/count/aggregation,
   source-corpus digest, source mapping digest, and OpenSearch green health.
6. Atomically move `documents-metadata-filter-current` to the new generation.
   If any check fails, leave the alias unchanged.
7. Retain the prior generation for atomic rollback; do not delete it in the
   activation transaction.

## Mapping

Use the repository mapping from `metadata_filter_projection_mapping()`:

- exact identities, periods, types, sources, actors, hashes and conflict codes
  are `keyword`;
- full calendar days are `date` with `strict_date`;
- presence, uncertainty and conflict flags are `boolean`;
- source-qualified temporal evidence is `nested`;
- detailed returned evidence is stored as `object` with `enabled:false`;
- the root and filter object use `dynamic:strict`;
- there is no `text`, `vector`, or `knn_vector` field.

All filter arrays mean “at least one preserved observation reports this
value”. They are not preferred truths and do not collapse conflicts.

## Cost evidence

The validated 100-row canary generated all 47,133 projections in memory with
zero generation failures in 219.883 seconds. DLS-scoped canary filters measured
roughly 3–5 ms p95 instead of the 216.77-second full application scan. The
full build still needs measured bulk throughput, verification latency, CPU,
RSS and disk footprint; no SLA is assumed from the canary.

## Rollback

Rollback is a single atomic alias action restoring the previous generation.
It does not touch raw metadata, content, chunk identities, embeddings, HNSW,
BM25 statistics, RRF, PROV-O, or coverage. A missing, stale, mismatched, or
unavailable projection remains fail-closed and is never replaced by a
production-query application-side scan.

## Search activation dependency

The internal phase-1 restriction primitive is implemented but must remain
disabled until the migration above has separate explicit approval and passes
the full DLS, freshness, lane-parity, no-filter GT1/GT2 and scope/coverage
gates. Natural-language filter extraction and metadata ranking boosts are out
of scope.

## Association activation dependency

The current combined `SAME_PARENT_COLLECTION` key is not activation-safe: its
largest bucket contains 30,901 occurrences because explicit `attachment_of`
parents and broad `member_of`/`contained_in` collections share one STRONG
dimension. The conservative wrapper rejects that dimension, source system,
source family, years, MIME/format, extension and collision-prone basename.
Neighborhood activation still requires a role-safe parent-policy change and
completed human labels; neither is part of this index migration.
