# A. Baseline

application_repo: FermedePommerieux/openrag-compose
target_branch: origin/pommerieux/v0.6.0-retrieval-v2-prov-o
starting_sha: 64a874c3719f2fa4cb0f4d4a61f0121539d8e8ab
worktree: /Users/eloiprimaux/Developer/openrag-compose-document-metadata-backfill (implementation); /Users/eloiprimaux/Developer/openrag-compose-metadata-full-backfill (execution evidence)
gitops_sha: 936964ca6cc5c0956ab04564add24dccc6327959
cluster: 10.73.50.12
ingress: https://openrag.ferme-de-pommerieux.fr

The dedicated baseline branch was created directly from the requested remote SHA. The stale local homonymous target branch and all protected worktrees were left unchanged. The full-run branch is `agent/metadata-full-backfill`; its runner-resume fix is commit `a2d3547c0be3c7691a444c44cbe093e4bb7adaba`.

# B. Archive identity mapping

indexed documents: 47,400 distinct documents / 47,454 occurrences
archive mapped: 47,362 distinct documents / 47,416 occurrences
hash verified: 47,362 distinct documents / 47,416 occurrences
ambiguous: 0
missing: 38
mapping coverage: 99.919831% of distinct documents / 99.919922% of occurrences

For an OpenArchiver attachment, `source_url` intentionally points to the archived parent-mail UI and is never used as the binary locator. The real original is resolved from the exact attachment `source_entity_id` to the connector's authoritative attachment object/storage locator, then admitted only after full SHA-256 and SHA-derived `document_id` verification. Filename-only and parent-mail URL matches fail closed.

The connector-side attachment ingestion contract is implemented without changing `source_url`. The dedicated archive-object resolver API is not yet implemented: this historical backfill used the explicitly transitional read-only manifest/registry adapter and authenticated internal download path. Replacing that private-schema dependency remains planned connector compliance work.

# C. Format inventory

The `documents` column is occurrence-level; successful profile counts later in this report are distinct-document-level.

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

`openrag.document-metadata v1` extends the existing documentary entity with separate `identity`, `embedded`, `filesystem`, `archive`, and `ingestion` observations. Every important value preserves raw value, normalized value, source, source type, trust class, extraction time, normalization version/status, and explicit timezone or `UNKNOWN`. The canonical `metadata_facts_sha256` excludes extraction wall-clock time. The profile is stored only on the `chunk_index=0` representative and is disabled for OpenSearch indexing and ranking.

# E. Metadata resolution policy

`openrag.metadata-resolution v1` preserves conflicting observations and materializes no preferred synthetic truth. The documented candidate order—format-native, archive-native, filesystem, inferred—is not applied automatically in v1. Trust records provenance, not truth. Embedded created/modified, filesystem birth/mtime/ctime, archive timestamps, and ingestion time remain distinct.

# F. PROV-O mapping

| metadata/source | PROV-O mapping | asserted/inferred | scope-expanding | reason |
|---|---|---|---|---|
| existing source entity id | existing `prov:Entity` enrichment | asserted | no | no parallel entity model |
| source-native `attachment_of` | existing bounded membership relation | asserted | existing policy unchanged | explicit parent mail |
| archive timestamps/object | descriptive archive facts | asserted about archive | no | not authorship facts |
| embedded author/creator/date | descriptive document facts | observed | no | may be false or spoofed |
| filesystem timestamps | descriptive filesystem facts | observed | no | copies can alter them |
| same author/date/name/software/folder/MIME | none | descriptive similarity | no | not proof |
| version/lineage guess | none in v1 | inferred | no | outside scope |

# G. DLS model

All v1 facts default to `internal_metadata`. The complete profile and control fields are recursively removed from public/model search and files payloads. Live post-run probes exposed no metadata profile, archive id/storage locator, hidden path, parent id, or backfill status. Writes require exact document generation, source occurrence, owner scope, and expected chunk count; mismatches return `DLS_BLOCKED`. Result: PASS, with 0 DLS-blocked full-run writes.

# H. Sample cohort

documents: 60 read-only extraction samples plus a 100-document production write canary
formats: EML, PDF, DOCX, XLSX, image, HTML, CSV, TXT
mapping status: 60/60 hash-verified in dry run; canary 100/100 verified after bounded recovery; OpenArchiver attachments explicitly represented

# I. Sample extraction results

success: 60/60 dry-run extractions; 100/100 production canary verifications
unchanged: 60/60 canonical digests on second extraction; 100/100 production records on idempotency run
unsupported: 0
ambiguous: 0
failed: 0 final canary failures

The canary also proved same-checkpoint resume, 10/10 rollback, immutable-field equality, and restoration.

# J. Conflict analysis

The full successful cohort contains 225 PDF Info/XMP conflicts, 306 creator/lastModifiedBy differences, 820 invalid timestamp observations, and 1,235 timestamps without a timezone. Embedded/archive date comparisons produced no direct normalized conflict in the captured policy. Conflicts and invalid values remain separate observations; none is silently promoted to truth.

# K. Idempotency

second run changed: 0 expected; observed 0/100 in the production canary. The full execution resumed after 25,900 durable records without duplicate profiles or edges. The 47,133 indexed representative profiles equal 47,130 successful selected distinct documents plus three pre-existing duplicate-occurrence canary/sample representatives.

# L. Performance

mean extraction: 1.488 ms/document
p95: 2.092 ms/document
I/O: 8,751,176,161 bytes read
estimated full corpus duration: actual active execution estimate 30,909.6 s (8 h 35 min); wall time including the fail-closed pause 39,441.4 s (10 h 57 min)

Mean end-to-end item time was 223.641 ms, p95 376.449 ms; effective active throughput was approximately 1.533 documents/s. OpenSearch stayed green with zero unassigned shards and no OOM event. The 3 GiB threshold was an intentionally conservative application guard, not the Kubernetes limit: the backend limit is 4 GiB. It preserved about 1 GiB headroom and exposed the runner's quadratic checkpoint reaggregation; resume now skips terminal batches and aggregates incrementally. The guard was not weakened.

# M. GT1 regression

GT1 q1 50/50, seed 100, RRF k=60 is valid 3/3. Strict seed component recall remains 36.0%; post-scope document/component recall is 45.8%/44.7%; precision is 77.3%; mean latency is 4.384 s. Within the new capture, seed and scope membership are stable across repetitions (Jaccard 1.0).

Against the exact pre-full capture, seed membership Jaccard is 0.980198 and scope membership Jaccard is 0.985507; seed and scope order are not identical. Therefore exact retrieval membership did not remain invariant, although recall stayed at its historical reference and the accepted dense contract is `APPROXIMATE_MEMBERSHIP`.

# N. GT2 regression

GT2 q1 50/50, seed 100, RRF k=60 is valid 3/3. Strict seed component recall remains 66.7%; post-scope document/component recall is 60.4%/66.7%; precision is 27.2%; mean latency is 4.582 s. Within the new capture, seed and scope membership are stable across repetitions (Jaccard 1.0).

Against the exact pre-full capture, seed membership Jaccard is 0.869159 and scope membership Jaccard is 0.838863; seed and scope order are not identical. This is material approximate-KNN membership drift and prevents a fully validated conclusion despite unchanged strict recall.

# O. Scope/coverage regression

`openrag.scope-coverage v1` passes 3/3 with complete coverage, empty frontier, unchanged contract configuration, and no unclassified relation. No metadata relation or weak inference expanded closure, and `coverage.complete` was not relaxed. Scope sets nevertheless changed because their ANN seed cohorts changed.

The evidence supports this inference: metadata-only update-by-query rewrote representative Lucene documents while carrying their unchanged vectors forward, which can change Lucene document ids/HNSW topology and approximate KNN membership. No embedding value, chunk content, or PROV-O edge changed. Result: `PASS_WITH_APPROXIMATE_KNN_DRIFT`.

# P. Full backfill gate

FULL_BACKFILL:
READY

The gate was validated before execution by the 100-document production canary: exact mapping, DLS, write/readback, checkpoint/resume, idempotency, rollback, resource sampling, GT checks, and immutable-field checks passed.

# Q. Full backfill execution

attempted: 47,400
success: 47,033 changed and verified
unchanged: 97
unsupported: 0
ambiguous: 0
missing: 38
failed: 232 metadata extraction failures, fail-closed
retry success: 0 document-level recoveries recorded

Total successful profiles: 47,130 (99.43038%). The first process stopped safely at its memory guard after 25,900 durable records; the corrected runner resumed from that checkpoint and completed all 474 batches.

# R. Post-backfill corpus integrity

document count: 47,400 distinct / 47,454 visible occurrences
chunk count: 380,817
embedding count: 380,817 (`chunk_embedding_text_embedding_3_large`)
corpus digest: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
OpenSearch: green, 0 unassigned shards

Document, occurrence, chunk, embedding counts and corpus identity digest are unchanged. Archive writes: 0. Chunk text/id/order/hash mutations: 0. Embedding-value mutations: 0. New PROV-O edges: 0.

# S. Metadata coverage

| format | profiles | embedded created | embedded modified | creator/author | lastModifiedBy | producer | application | archive facts | conflicts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CSV | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 0 |
| DOCX | 421 | 420 | 417 | 408 | 407 | 0 | 420 | 421 | 0 |
| EML | 40,885 | 0 | 0 | 0 | 0 | 0 | 0 | 40,885 | 0 |
| HTML | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 0 |
| image | 22 | 1 | 1 | 0 | 0 | 0 | 1 | 22 | 0 |
| PDF | 5,351 | 4,722 | 3,752 | 2,194 | 0 | 4,622 | 3,705 | 5,351 | 225 |
| TXT | 62 | 0 | 0 | 0 | 0 | 0 | 0 | 62 | 0 |
| XLSX | 326 | 311 | 322 | 264 | 286 | 0 | 323 | 326 | 0 |
| total | 47,130 | 5,454 | 4,492 | 2,866 | 693 | 4,622 | 4,449 | 47,130 | 225 |

Filesystem metadata is 0 because the read-only archive interfaces did not provide trustworthy filesystem observations for this cohort. EML sender and sent time remain email-specific observations rather than being conflated with author or document creation. Parent entity ids exist on 5,969 profiles. The 270 unprofiled documents comprise 38 missing archive sources and 232 unreadable/malformed originals.

# T. Product changes

retrieval defaults changed: no
chunking changed: no
embeddings changed: no
GitOps changed: yes
deployment: yes

The deployed code is backward-compatible and default-neutral; the historical full backfill remained an explicit operator action.

# U. Production

source_sha: bfbf3622b84deb234709a5c991b3bbbc51ab4bc7
backend_tag: v0.6.0-retrieval-v2.85
backend_digest: sha256:39186b9f6e0f418e485dd1764d16254c1809398c02215da54a838989ae114a92
gitops_sha: d5f544adccb324e2035b7e8da8a411c5c2cddb54
Fleet: Ready, 7/7; five cluster nodes Ready; backend 1/1; Langflow 3/3; frontend 1/1; connector 1/1; OpenSearch green
RuntimeBehavior: MATCH (`q1`, lexical 50, dense 50, seed 100, RRF k=60, multi-query false)

The connector source is `6b4741d1861e7d24537f5d6194d056afb6975972`, digest `sha256:b27baddc6be475a3e2c45a48810c54ae1eec06953054ae4e9748d2e40b8b8ba5`. GitOps owns neither functional model selection nor system prompt.

# V. Tests

The full unit suite passes (1,685 tests; 94 warnings). Metadata mapping/extraction/normalization/timezone/conflict/digest/idempotency/DLS/PROV-O non-expansion/coverage/dry-run/resume/malformed-input tests pass. The production canary proved live checkpoint, rollback, restoration, and second-run no-op behavior. Ruff, Mypy on changed Python files, `uv lock --check`, JSON validation, and `git diff --check` pass. Helm rendering was not required for the final full-run branch because it changed no deployment manifest.

# W. Remaining risks

- Embedded metadata may be false or deliberately spoofed.
- Timestamps may conflict across embedded, filesystem and archive sources.
- Filesystem timestamps may reflect copies rather than authorship.
- Weak version inference is not trusted and remains non-scope-expanding.
- The historical mapping/audit path still depends on a read-only private OpenArchiver manifest; it should migrate to the planned connector resolver contract.
- 232 originals could not be parsed and remain fail-closed; 38 have no validated archive source.
- In-place updates of vector-bearing Lucene documents can perturb approximate KNN membership even when vector values are unchanged; a future update strategy must account for this.
- The 3 GiB application guard is a conservative operational policy under a 4 GiB container limit, not a proof that 3 GiB is universally optimal.

# X. Next possible chantier

Candidates: implement the internal connector resolver API and retire the historical private-manifest dependency; evaluate an index-update strategy that avoids ANN topology perturbation; metadata-aware retrieval; version lineage inference; document chronology reasoning; metadata filters; date-aware investigative tools. None is started automatically.

# Y. Qwen

QWEN_READINESS:
BLOCKED

# Z. Conclusion

DOCUMENT METADATA BACKFILL PARTIAL
