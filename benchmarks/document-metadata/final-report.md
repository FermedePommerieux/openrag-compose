# A. Baseline

application_repo: FermedePommerieux/openrag-compose
target_branch: origin/pommerieux/v0.6.0-retrieval-v2-prov-o
starting_sha: 64a874c3719f2fa4cb0f4d4a61f0121539d8e8ab
worktree: /Users/eloiprimaux/Developer/openrag-compose-document-metadata-backfill
gitops_sha: 936964ca6cc5c0956ab04564add24dccc6327959
cluster: 10.73.50.12
ingress: https://openrag.ferme-de-pommerieux.fr

Dedicated branch: `agent/document-metadata-backfill`; implementation commit: `68d78552`. The protected local target branch and protected worktrees were not changed.

# B. Archive identity mapping

indexed documents: 47,400 distinct documents / 47,454 occurrences
archive mapped: 47,362 distinct documents / 47,416 occurrences
hash verified: 47,362 distinct documents / 47,416 occurrences
ambiguous: 0
missing: 38
mapping coverage: 99.919831% of distinct documents / 99.919922% of occurrences

An OpenArchiver attachment keeps the parent-mail UI in `source_url`. Its binary is resolved only by exact attachment `source_entity_id`, a validated read-only registry entry, internal `storage_path`, size, full SHA-256 and SHA-derived `document_id`. `source_url` and filename are never binary mapping proof.

# C. Format inventory

The `documents` column is occurrence-level because one content document can have multiple source occurrences.

| format | documents | archive available | extractor | supported |
|---|---:|---:|---|---|
| PDF | 5,468 | 5,437 | Info dictionary + XMP | yes |
| DOCX | 428 | 425 | OOXML core/app | yes |
| XLSX | 330 | 328 | OOXML core/app | yes |
| PPTX | 0 | 0 | OOXML core/app | yes, generic |
| ODT | 0 | 0 | ODF meta.xml | yes, generic |
| ODS | 0 | 0 | ODF meta.xml | yes, generic |
| ODP | 0 | 0 | ODF meta.xml | yes, generic |
| images | 22 | 22 | EXIF/IPTC/bounded XMP | yes |
| EML | 41,080 | 41,079 | bounded RFC 5322 headers | yes |
| MSG | 0 | 0 | none v1 | no |
| HTML | 35 | 34 | identity/archive facts | yes |
| CSV | 29 | 29 | identity/archive facts | yes |
| TXT/ASCIIDOC | 62 | 62 | identity/archive facts | yes |
| ZIP | 0 | 0 | none v1 | no |
| other | 0 | 0 | none v1 | no |

# D. DocumentMetadataProfile v1

`openrag.document-metadata v1` extends the existing documentary entity. It contains separate `identity`, `embedded`, `filesystem`, `archive`, and `ingestion` observations. Every important value stores its raw value, normalized value, source, source type, trust class, extraction time, normalization version/status and explicit timezone or `UNKNOWN`. The canonical `metadata_facts_sha256` excludes extraction wall-clock time. The profile is stored on the unique `chunk_index=0` representative only and is disabled for OpenSearch indexing/ranking.

# E. Metadata resolution policy

`openrag.metadata-resolution v1` preserves conflicting observations and materializes no preferred synthetic value. Format-native > archive-native > filesystem > inferred is documented only, not applied. Trust class records origin, not truth. Timestamps remain source-qualified and are never collapsed to one `created_at`.

# F. PROV-O mapping

| metadata/source | PROV-O mapping | asserted/inferred | scope-expanding | reason |
|---|---|---|---|---|
| existing source entity id | existing `prov:Entity` enrichment | asserted | no | no second entity |
| source-native `attachment_of` | existing bounded membership relation | asserted | existing policy unchanged | explicit parent mail |
| archive timestamps/object | descriptive archive facts | asserted about archive | no | not authorship facts |
| embedded author/creator/date | descriptive document facts | observed | no | can be false/spoofed |
| filesystem timestamps | descriptive filesystem facts | observed | no | copies can alter them |
| same author/date/name/software/folder/MIME | none | descriptive similarity | no | not proof |
| version/lineage guess | none in v1 | inferred | no | outside scope |

# G. DLS model

All v1 facts default to `internal_metadata`. The complete profile and its control fields are recursively removed from model/public search payloads. Archive ids, storage locators, parent ids, filenames, paths and user-bearing metadata cannot cross DLS boundaries. Writes require exact document generation, source occurrence and owner scope plus the expected full chunk count; any mismatch returns `DLS_BLOCKED`.

# H. Sample cohort

documents: 60
formats: EML 10, PDF 18, DOCX 11, XLSX 8, image 5, HTML 3, CSV 2, TXT 3
mapping status: 60 hash-verified; OpenArchiver 44, local archive 16; 34 email attachments

# I. Sample extraction results

success: 60
unchanged: 0 on first dry run; 60 digest-identical on second extraction
unsupported: 0
ambiguous: 0
failed: 0

This was a dry-run: `would_update=60`, index writes 0.

# J. Conflict analysis

The real size-bounded sample contained 0 conflicts. Synthetic/adversarial tests preserve PDF Info/XMP disagreements, invalid dates, absent timezones, creator/lastModifiedBy differences and malformed metadata as separate observations or explicit failures. No preferred truth is silently selected.

# K. Idempotency

second run changed: 0 expected; observed 0/60 digest changes. Tests also require one representative update and no duplicate graph nodes or edges.

# L. Performance

mean extraction: 1.564 ms/document
p95: 0.832 ms/document (three high outliers, max 59.018 ms, lift the mean)
I/O: 492,532 bytes in the deliberately size-capped sample; safe full cohort is 8,318,533,115 bytes
estimated full corpus duration: not reliably estimable from this sample; large-file download and write canaries remain required

# M. GT1 regression

q1 50/50, seed 100, RRF k=60: 3/3 valid. Seed membership Jaccard 1.0; lower-rank seed order differs within the accepted `APPROXIMATE_MEMBERSHIP` dense KNN contract. Scope membership/order, coverage facts, recall and precision are unchanged. Mean latency: 4.277 s.

# N. GT2 regression

q1 50/50, seed 100, RRF k=60: 3/3 valid. Seed membership/order, scope membership/order, coverage facts, recall and precision are unchanged. Mean latency: 3.781 s.

# O. Scope/coverage regression

PASS. `openrag.scope-coverage v1` remains valid; `coverage.complete` was not relaxed. Metadata creates no closure expansion and existing strong documentary relations remain unchanged.

# P. Full backfill gate

FULL_BACKFILL:
BLOCKED

Missing gates: production write/rollback/checkpoint canary, representative 8.32 GB resource estimate, and connector resolver API or proven transitional adapter operation in production.

# Q. Full backfill execution

NOT EXECUTED

# R. Post-backfill corpus integrity

document count: 47,400 distinct / 47,454 occurrences
chunk count: 380,817
embedding count: 380,817 (`chunk_embedding_text_embedding_3_large`)
corpus digest: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
OpenSearch: green

This is a current no-write integrity capture, not a post-full-backfill result.

# S. Metadata coverage

Sample-level only: all 60 have identity/archive/ingestion facts; embedded created 31, embedded modified 28, creator 17, lastModifiedBy 17, producer 14, creator application 21, parent entity ids 34, filesystem facts 0, conflicts 0. By format: PDF created 14/18, modified 11/18, producer 14/18; DOCX created/modified/creator/lastModifiedBy 11/11; XLSX 6/8 for those four fields; EML sender/sent date 10/10. Images and plain formats in this sample expose no non-sensitive embedded fields.

# T. Product changes

retrieval defaults changed: no
chunking changed: no
embeddings changed: no
GitOps changed: no
deployment: no

# U. Production

source_sha: 36c5afdec6a09ea809a9a0e733de191de1b578c7
backend_tag: v0.6.0-retrieval-v2.83
backend_digest: sha256:9b482b41589c7e8456a926e52d8ae3f5a7c44b2ffee3d78b7df181bc4d69c083
gitops_sha: 936964ca6cc5c0956ab04564add24dccc6327959
Fleet: Ready
RuntimeBehavior: MATCH

# V. Tests

37 targeted metadata/mapping/checkpoint tests pass; 1,658 unit tests pass with 94 pre-existing warnings. Ruff lint, Mypy on changed sources/scripts/tests, `uv lock --check`, JSON validation and `git diff --check` pass. No Helm render is required because deployment manifests did not change. GitHub CI run 33598896651 terminated with an infrastructure failure: SDK jobs report a missing/invalid API key, E2E jobs report `OPENAI_API_KEY is not set`, and core spent 2 h 18 retrying Langflow authentication (`401`/`429`) before system-flow startup failed. The backend source image was not built in this `use_local_images=false` run, and no reported failure traverses the metadata-backfill code.

# W. Remaining risks

- Embedded metadata may be false or deliberately spoofed.
- Timestamps may conflict across embedded, filesystem and archive sources.
- Filesystem timestamps may reflect copies rather than authorship.
- Weak version inference is not trusted and remains non-scope-expanding.
- The current transitional OpenArchiver adapter reads the connector registry in read-only mode; the planned internal resolver API must remove this private-schema dependency before broad operationalization.
- Large files and production writes have not yet established a Pi-safe end-to-end resource envelope.

# X. Next possible chantier

First candidate: OpenArchiver archive-object resolver API compliance and a controlled production metadata write/rollback canary. Later candidates: metadata-aware retrieval, version lineage inference, document chronology reasoning, metadata filters, and date-aware investigative tools. None is started automatically.

# Y. Qwen

QWEN_READINESS:
BLOCKED

# Z. Conclusion

DOCUMENT METADATA BACKFILL PARTIAL
