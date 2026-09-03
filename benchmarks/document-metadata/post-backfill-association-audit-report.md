## A. Baseline

application_repo: `FermedePommerieux/openrag-compose`

target_branch: `pommerieux/v0.6.0-retrieval-v2-prov-o`

starting_sha: `b8d4325be2a9e7dae3acad72587d1c3577fa11b1` (local integrated baseline: full backfill `5dd6c912` plus semantics cherry-pick)

worktree: `/Users/eloiprimaux/Developer/openrag-compose-post-backfill-association-audit`

work_branch: `agent/post-backfill-association-audit`

final_sha: reported in the final delivery because this report is part of the final commit.

Git verification passed: the merge-base of `5dd6c912` and `c99860a7` is `affca149dce16eaa08d0288b12d23e85c357e74a`; the semantics cherry-pick added its five new files without conflict. Existing worktrees were preserved. The integrated baseline was pushed before the audit.

## B. Corpus state

documents: 47,400 distinct binaries

occurrences: 47,454 source occurrences

chunks: 380,817

embeddings: 380,817 in `chunk_embedding_text_embedding_3_large`; 0 in legacy `chunk_embedding`

metadata profiles: 47,133 occurrence profiles across 47,132 distinct binaries

OpenSearch: green before and after, 0 unassigned shards

corpus digest: `038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7`

The historical job outcome remains 47,033 enriched, 97 unchanged, 232 extraction-impossible and 38 archive/source-unavailable. The apparent 47,130-versus-47,132 difference is real and reconciled: three profiles existed on duplicate occurrences before the selected distinct-document run; two of those occurrences cover binaries outside its 47,130-success set. Consequently, 268 binaries currently have no profile, while the 270 historical non-enriched selected records remain an `UNKNOWN`/`UNAVAILABLE` status cohort, never a negative fact.

## C. Metadata coverage

All counts below are distinct binaries with a queryable profile, denominator 47,400. Source-qualified raw observations remain internal and no global truth field was synthesized.

| field | available |
|---|---:|
| embedded_created | 5,455 |
| embedded_modified | 4,493 |
| creator/author | 2,867 |
| lastModifiedBy | 694 |
| producer | 4,622 |
| creator_application | 4,450 |
| archive timestamps | 0 |
| archive source | 47,132 |
| explicit parent entity | 5,970 |
| parent collection | 47,129 |
| source system | 47,129 |
| source entity family | 47,129 |
| MIME | 47,132 |
| format | 47,132 |
| extension | 47,132 |
| filename basename | 47,132 |
| binary SHA | 47,132 |
| at least one explicit-offset timestamp | 47,132 |
| at least one timezone-unknown timestamp | 417 |
| at least one invalid timestamp | 820 |
| at least one investigation conflict | 1,661 |

Coverage by historical extension-based format:

| format | profiles | created | modified | creator | modifier | producer | application | parent | tz unknown | invalid | conflicts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CSV | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 28 | 0 | 0 | 0 |
| DOCX | 421 | 420 | 417 | 408 | 407 | 0 | 420 | 359 | 0 | 0 | 207 |
| EML | 40,886 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 72 | 0 | 72 |
| HTML | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 0 | 0 | 0 |
| IMAGE | 22 | 1 | 1 | 0 | 0 | 0 | 1 | 0 | 1 | 0 | 1 |
| PDF | 5,351 | 4,722 | 3,752 | 2,194 | 0 | 4,622 | 3,705 | 5,190 | 344 | 820 | 1,285 |
| TXT | 62 | 0 | 0 | 0 | 0 | 0 | 0 | 62 | 0 | 0 | 0 |
| XLSX | 327 | 312 | 323 | 265 | 287 | 0 | 324 | 297 | 0 | 0 | 96 |

Coverage by source system:

| source system | profiles | created | modified | creator | modifier | producer | application | parent | tz unknown | invalid | conflicts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| openarchiver | 46,856 | 5,211 | 4,256 | 2,720 | 602 | 4,478 | 4,239 | 5,970 | 411 | 728 | 1,532 |
| local | 273 | 241 | 237 | 147 | 92 | 141 | 208 | 0 | 6 | 89 | 126 |
| UNKNOWN provenance | 3 | 3 | 0 | 0 | 0 | 3 | 3 | 0 | 0 | 3 | 3 |

Coverage by document source type is: `email_message` 40,886 profiles, `email_attachment` 5,970, `file` 273, and `UNKNOWN` 3. The full field × format × source-system × source-type counters are in `post-backfill-association-audit.json`.

## D. Temporal coverage

These are observation counts, not preferred document timestamps.

| role / basis / granularity | observations | distinct periods | range |
|---|---:|---:|---|
| production / source-local / day | 47,423 | 5,157 | 1996-05-06 → 2026-08-29 |
| production / source-local / month | 47,423 | 221 | 1996-05 → 2026-08 |
| production / source-local / year | 47,423 | 26 | 1996 → 2026 |
| production / UTC / day | 47,014 | 5,156 | 1996-05-06 → 2026-08-29 |
| production / UTC / month | 47,014 | 220 | 1996-05 → 2026-08 |
| production / UTC / year | 47,014 | 25 | 1996 → 2026 |
| modification / source-local / day | 5,482 | 2,088 | 1996-05-06 → 2026-08-25 |
| modification / source-local / month | 5,482 | 206 | 1996-05 → 2026-08 |
| modification / source-local / year | 5,482 | 22 | 1996 → 2026 |
| modification / UTC / day | 5,439 | 2,069 | 1996-05-06 → 2026-08-25 |
| modification / UTC / month | 5,439 | 205 | 1996-05 → 2026-08 |
| modification / UTC / year | 5,439 | 22 | 1996 → 2026 |

| observation source | production | modification | other temporal role |
|---|---:|---:|---:|
| PDF Info | 4,679 | 3,642 | 0 |
| XMP (PDF + image) | 1,924 | 1,824 | 0 |
| OOXML core | 732 | 740 | 0 |
| EML header | 40,880 | 0 | 0 |
| EXIF | 0 | 1 | 0 |
| archive | 0 | 0 | 0 |
| filesystem | 0 | 0 | 0 |
| ingestion | 0 | 0 | 47,132 |

The artifact includes each full day/month/year distribution by exact observation source. EML `Date` remains an EML-header production observation. Ingestion remains ingestion. No `document.created_at` was created.

## E. Timezone distribution

| status | observations |
|---|---:|
| EXPLICIT_OFFSET | 99,585 |
| UNKNOWN | 452 |
| INVALID | 1,517 |
| ASSUMED_BY_FORMAT | 0 |

By source: PDF Info has 6,472 explicit, 360 unknown, 1,489 invalid; PDF XMP 3,701 explicit, 17 unknown, 28 invalid; OOXML core 1,472 explicit; EML headers 40,808 explicit and 72 unknown; image EXIF 1 unknown; image XMP 2 unknown; ingestion 47,132 explicit.

Among observations where both calendars exist, source-local month differs from UTC month 55 times out of 52,453, and source-local year differs from UTC year 8 times out of 52,453. Filters must therefore declare `SOURCE_LOCAL` or `UTC`; timezone-unknown observations never acquire a UTC value.

## F. Conflict distribution

| conflict | documents | conflict observations |
|---|---:|---:|
| multiple creation observations | 143 | 143 |
| multiple modification observations | 116 | 116 |
| PDF Info/XMP disagreement | 225 | captured as 440 `SOURCE_CONFLICT` observations |
| creator vs lastModifiedBy | 302 | 302 |
| modified before created | 71 | 71 |
| archive/embedded inversion | 0 | 0 |
| invalid timestamp | 820 | 1,517 |
| timezone unknown | 417 | 452 |
| future timestamp | 0 | 0 |

No conflict was resolved and no preferred value was selected.

## G. Association key inventory

Association keys are occurrence-scoped: 47,133 profiled source occurrences, not collapsed binaries. All existing `AssociationDimension` values were audited. `SAME_SOURCE_ENTITY_FAMILY` and `SAME_DOCUMENT_TYPE` have zero populated v1 keys, even though source provenance exposes three source entity families. An offline, non-policy-changing structural inventory measured those provenance values separately.

Observed structural inventory: 5,722 explicit-parent buckets, 32,953 collection-membership buckets, 3 source-family buckets, and 2 source-system buckets. Existing key details and audit-only structural details are separate in the JSON artifact.

## H. Bucket cardinalities

| dimension | buckets | count | min | p50 | p75 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| production_day | 5,157 | 45,664 | 1 | 7 | 13 | 19 | 24 | 35 | 142 |
| production_month | 221 | 45,664 | 1 | 201 | 302 | 429 | 495 | 616 | 751 |
| production_year | 26 | 45,663 | 1 | 1,220 | 3,216 | 4,636 | 4,950 | 5,134 | 5,134 |
| modification_day | 2,088 | 3,882 | 1 | 1 | 2 | 3 | 4 | 8 | 39 |
| modification_month | 206 | 3,881 | 1 | 16 | 28 | 41 | 48 | 56 | 72 |
| modification_year | 22 | 3,879 | 1 | 179 | 304 | 435 | 478 | 478 | 478 |
| source_system (v1 key) | 3 | 47,406 | 273 | 276 | 46,857 | 46,857 | 46,857 | 46,857 | 46,857 |
| source_entity_family (v1 key) | 0 | 0 | — | — | — | — | — | — | — |
| source_entity_family (observed) | 3 | 47,130 | 273 | 5,971 | 40,886 | 40,886 | 40,886 | 40,886 | 40,886 |
| explicit parent `attachment_of` | 5,722 | 7,293 | 1 | 1 | 1 | 2 | 2 | 5 | 45 |
| parent collection membership | 32,953 | 94,577 | 1 | 1 | 1 | 2 | 3 | 7 | 30,901 |
| parent collection (combined v1 key) | 38,675 | 101,870 | 1 | 1 | 1 | 2 | 3 | 7 | 30,901 |
| MIME | 7 | 47,133 | 29 | 327 | 5,374 | 40,886 | 40,886 | 40,886 | 40,886 |
| format family | 5 | 47,071 | 34 | 421 | 5,374 | 40,886 | 40,886 | 40,886 | 40,886 |
| explicit document type | 0 | 0 | — | — | — | — | — | — | — |
| creator observation | 916 | 2,898 | 1 | 1 | 2 | 4 | 8 | 25 | 296 |
| last modifier observation | 184 | 694 | 1 | 1 | 2 | 4 | 7 | 113 | 200 |
| producer observation | 847 | 4,694 | 1 | 2 | 4 | 10 | 19 | 84 | 256 |
| creator application observation | 596 | 4,482 | 1 | 2 | 4 | 12 | 28 | 115 | 337 |
| filename basename | 46,569 | 47,133 | 1 | 1 | 1 | 1 | 1 | 1 | 62 |
| binary hash | 47,132 | 47,133 | 1 | 1 | 1 | 1 | 1 | 1 | 2 |

UTC day/month/year variants are independently recorded and differ from source-local values. For every dimension, the audit JSON additionally records singleton counts and bucket counts over 10, 50, 100, 500, 1,000 and 5,000. Examples: filename has 46,211 singleton buckets; production year has 13 buckets over 1,000 and one over 5,000; combined parent has 30,259 singletons but two buckets over 5,000; binary hash has 47,131 singletons.

## I. Mega-hub classification

| dimension | max bucket | p95 | classification | recommended standalone use |
|---|---:|---:|---|---|
| SAME_PRODUCTION_YEAR | 5,134 | 4,950 | MEGA_HUB_PRONE | no |
| SAME_PRODUCTION_MONTH | 751 | 495 | MEGA_HUB_PRONE | no |
| SAME_SOURCE_SYSTEM | 46,857 | 46,857 | NOT_USEFUL_ALONE | no |
| SAME_MIME_TYPE | 40,886 | 40,886 | NOT_USEFUL_ALONE | no |
| COMPATIBLE_DOCUMENT_TYPES | 40,886 | 40,886 | NOT_USEFUL_ALONE | no |
| SAME_CREATOR_OBSERVATION | 296 | 8 | USABLE_WITH_BOUNDS | no; companion evidence only |
| SAME_PRODUCER_OBSERVATION | 256 | 19 | USABLE_WITH_BOUNDS | no; companion evidence only |
| SAME_PARENT_COLLECTION (combined v1) | 30,901 | 3 | NOT_USEFUL_ALONE | no |
| explicit parent `attachment_of` (audit split) | 45 | 2 | DISCRIMINATING | yes, after role-safe policy split |
| source entity family (observed) | 40,886 | 40,886 | NOT_USEFUL_ALONE | no |

The conceptual ordering is confirmed by maximum bucket size: explicit parent 45 < source family 40,886 < source system 46,857. A broad collection is a separate case and must not inherit explicit-parent strength.

## J. Hash/occurrence findings

distinct SHA-256 values: 47,400

hashes with more than one occurrence: 54

max occurrences per hash: 2

duplicate occurrences beyond the first: 54

The artifact contains ten safe examples of the same binary appearing once as a local file and once as an OpenArchiver attachment, with hashed occurrence identities. Occurrences were not collapsed. Among profiled occurrences, the binary-hash association inventory has one bucket of size 2 and 47,131 singleton buckets.

## K. Filename findings

Normalized basenames: 46,569 across 47,133 profiled occurrences. There are 358 collision buckets and 922 occurrences in them, a 1.9562% collision rate. The largest observed buckets are `signature` 62, `bulletins_originaux` 35, `notificationfacture` 22, `facture` 12, then `classeur1`, `webpage`, `scan`, `pri0130`, `ferme de p.`, `grf-cert_depot` and `sans titre` at 5.

Basename-only evidence is `USABLE_WITH_BOUNDS`, never sufficient alone. The names were discovered from the corpus; none is hardcoded into policy.

## L. Creator/producer findings

| observation | raw unique | normalized unique | collision groups | raw values joined | max bucket |
|---|---:|---:|---:|---:|---:|
| creator | 939 | 916 | 21 | 23 | 296 |
| last modifier | 196 | 184 | 12 | 12 | 200 |
| producer | 847 | 847 | 0 | 0 | 256 |
| creator application | 597 | 596 | 1 | 1 | 337 |

Normalization is exactly NFKC + whitespace collapse + casefold. No fuzzy match or entity resolution is performed. All introduced collisions are explainable by those exact transforms; the audit detected no fuzzy or cross-identity merge mechanism.

## M. Association strength distribution

The deterministic bounded sample considered 20,000 unique pairs and emitted 20,000 associations:

| strength | associations |
|---|---:|
| STRONG | 14,988 |
| MEDIUM | 1,274 |
| WEAK | 3,475 |
| VERY_WEAK | 263 |
| NONE | 0 |

This is an instrumented bounded population, not an unbiased all-pairs prevalence estimate. `SAME_PARENT_COLLECTION` appears in 14,987 emitted associations, explaining almost all `STRONG` results. Other frequent dimensions include source system 17,518, production year 12,391, production month 9,536, compatible type 14,071 and MIME 14,094.

## N. Candidate-generation scale

candidate pairs: 30,255 pair visits; 20,000 unique candidate pairs; 20,000 associations emitted

truncations: 14 dimensions truncated; at least 2,552,700,519 dimension-level theoretical pairs were not enumerated

largest bounded bucket: 51 retained members

all-pairs used: NO

Generation was capped at 25 pairs per bucket and 20,000 unique pairs globally. No `47,400 × 47,400` operation exists in the implementation or trace.

## O. Neighborhood cohort

seeds: 20, deterministically selected by SHA-256 strata (`openrag-post-backfill-cohort-v1`)

formats: PDF 8, EML 7, DOCX 1, XLSX 1, IMAGE 1, HTML 1, TXT 1

sources: OpenArchiver 18, local 2; source types include email attachments, email messages and files; years span 1996–2026; metadata-rich, metadata-poor and conflicting seeds are included.

## P. Neighborhood results

All other bounds were explicit: max associations 100, per-dimension limit 25, no source-scope override, no implicit time window.

| configuration | runs | exact candidates min / mean / max | returned per seed | strength summary | truncated runs | latency mean / p95 / max |
|---|---:|---:|---:|---|---:|---|
| N1 (`max_documents=10`) | 20 | 5,466 / 43,016.65 / 47,054 | 9 | 180 STRONG | 20/20 | 97.42 / 201.97 / 238.91 ms |
| N2 (`max_documents=25`) | 20 | 5,466 / 43,016.65 / 47,054 | 24 | 480 STRONG | 20/20 | 99.15 / 205.86 / 237.40 ms |
| N3 (`max_documents=50`) | 20 | 5,466 / 43,016.65 / 47,054 | 49 | 923 STRONG, 3 MEDIUM, 35 WEAK, 19 VERY_WEAK | 20/20 | 101.55 / 216.25 / 233.84 ms |

The bounded builder is fast, but candidate sets are dominated by mega-hubs and every run saturates its document cap. This is a quality-policy blocker, not a latency or DLS failure.

## Q. Human-review artifact

Path: `/Users/eloiprimaux/Developer/openrag-compose-post-backfill-association-audit/benchmarks/document-metadata/association-neighborhood-human-review.csv`

The file contains 480 seed → neighbor rows from N2. `human_judgment` and `human_note` are blank on every row. Suggested labels are `USEFUL`, `MARGINAL`, `NOT_USEFUL`. This artifact is not qrels and is separate from GT1/GT2.

## R. DLS neighborhood validation

PASS

Five controlled real-corpus partitions contain 35–145 hidden candidates each. In all five, hidden inputs are absent from output, do not change output or truncation, do not affect the DLS-scoped candidate count, and never surface an association. DLS filtering precedes bucketing.

## S. Metadata filter contract

contract: `openrag.metadata-filter v1`

The internal Pydantic contract and pure evaluator support `EQUAL`, `IN`, `BETWEEN`, `BEFORE`, `AFTER`, `EXISTS` and `NOT_EXISTS`; `ALL`/`ANY` conjunctions; canonical serialization and hashing; source-local/UTC calendar basis; semantic-role or explicit-source date policy; exact normalized structural/actor matching; and `MetadataFilterEvaluation` evidence with result, matched observations, conflicting observations, availability, policy id and version.

It is not wired to `/api/search`, a planner, OpenSearch writes, or an LLM.

## T. Three-valued logic

`TRUE`: the clause is supported by at least one valid matching observation, or an `EXISTS` query has a known valid value.

`FALSE`: complete available evidence is known and does not match, or a field is known absent for `EXISTS`.

`UNKNOWN`: the profile/field is unavailable, only invalid evidence exists, or the requested UTC projection cannot be established because timezone is unknown.

Only `TRUE` is eligible. Negation uses strong Kleene logic: `NOT TRUE = FALSE`, `NOT FALSE = TRUE`, and critically `NOT UNKNOWN = UNKNOWN`. Therefore `production_month != 2024-03` does not admit missing metadata. `NOT_EXISTS` is `TRUE` only for known absence; unavailable extraction remains `UNKNOWN`.

For `ALL`: any false makes false; otherwise an unknown makes unknown. For `ANY`: any true makes true; otherwise an unknown makes unknown.

## U. Temporal filter semantics

Supported semantic roles are production and modification at day, month and year granularity. Every temporal clause must state `SOURCE_LOCAL` or `UTC` and either `ANY_VALID_PRODUCTION_OBSERVATION`, `ANY_VALID_MODIFICATION_OBSERVATION`, or `EXPLICIT_SOURCE` with its source name.

`ANY_VALID_*` means one valid matching observation is sufficient for `TRUE`; other differing observations remain in `conflicting_observations`. Thus PDF Info March plus XMP April can be `TRUE` for March and simultaneously conflicting. `EXPLICIT_SOURCE` targets e.g. `pdf_xmp` without selecting it as a global preferred truth. Invalid observations never match. Unknown timezones support source-local calendar filters but produce `UNKNOWN` for UTC filters.

Human phrases such as “date du document”, “documents de mars”, “créés en 2024” and “version de 2023” require clarification or an explicitly documented broad role policy. They must not silently map to one magical date field.

## V. Type/source/creator filter semantics

MIME is exact after media-type normalization; format family and extension are deterministic; explicit source document type is accepted only when present, never inferred as genre. Source system, source entity family, parent collection and connector are exact structural values. Internal archive locators are not filter inputs or public evidence.

Creator, last modifier, producer and creator application support exact NFKC/space/case-normalized observation matches only. No fuzzy identity resolution or entity inference exists. Independently complete indexed structural context may produce TRUE/FALSE even when extraction failed; unavailable profile-only fields stay UNKNOWN.

## W. Real corpus filter cardinalities

Cardinalities are on 47,400 distinct binaries using one deterministic occurrence profile per binary plus fail-closed context. All rows partition exactly into TRUE/FALSE/UNKNOWN.

| structured filter | candidates | TRUE | FALSE | UNKNOWN | conflict evidence | mean / p95 latency per document |
|---|---:|---:|---:|---:|---:|---|
| PDF + production month 2024-03, any valid production observation, source-local | 47,400 | 44 | 45,968 | 1,388 | 794 | 0.188 / 0.241 ms |
| XLSX + modification year 2023, source-local | 47,400 | 49 | 47,344 | 7 | 734 | 0.139 / 0.162 ms |
| OpenArchiver + creator exists | 47,400 | 2,720 | 44,412 | 268 | 273 | 0.157 / 0.183 ms |
| production month 2024-03 + PDF format | 47,400 | 44 | 45,968 | 1,388 | 794 | 0.139 / 0.156 ms |
| production timestamp exists | 47,400 | 45,662 | 797 | 941 | 792 | 0.078 / 0.087 ms |
| PDF XMP production month 2024-03, explicit source | 47,400 | 16 | 43,855 | 3,529 | 14 | 0.137 / 0.158 ms |

The 232 extraction-impossible and 38 archive-unavailable historical records are never interpreted as false for unavailable fields. Two have alternate pre-existing occurrence profiles, a state-ledger reconciliation that must be preserved in any indexed projection.

## X. Filter implementation options

| option | readiness | advantages | limits |
|---|---|---|---|
| application-side scan | semantics validated, operationally unsuitable | no mapping change; easiest evidence projection | full representative scan took 216.77 s; profile parsing 74.13 s CPU-equivalent; cannot efficiently restrict lexical/KNN retrieval |
| OpenSearch normalized fields | recommended future path | same-query DLS + deterministic restriction + lexical/KNN filtering; scalable aggregations | requires a mapping proposal, reindex/projection backfill, occurrence-state reconciliation and exposure review |
| side index | possible fallback | isolates derived metadata and can be rebuilt | DLS/refresh synchronization, join/large-ID-set cost, occurrence identity complexity |
| runtime fields | not viable as-is | avoids stored duplication in simpler mappings | raw `document_metadata_profile` is `enabled:false`, so values are unavailable to queries/scripts; runtime parsing would also be expensive |
| existing fields only | partial structural subset | source system/type, connector, filename and MIME are already independently queryable | no source-qualified temporal/creator values; cannot satisfy v1 evidence semantics |

A future mapping should retain the raw profile as disabled internal metadata and add a versioned normalized projection. Temporal observations should be nested and preserve role, source, timezone status, source-local day/month/year, UTC day/month/year and internal observation id. Actor observations should preserve field/source/exact normalized value. Availability/conflict flags must be explicit. Public responses must expose only safe match evidence, never archive locators or raw internal metadata. To restrict BM25 and ANN directly, normalized filter fields must be present on every searchable chunk or resolved through a separately proven occurrence-ID restriction strategy; representative-only fields are insufficient.

## Y. Recommended production architecture

Use: explicit structured user constraint → deterministic parser/validator → DLS-scoped visible occurrence set → normalized OpenSearch metadata restriction → lexical and dense retrieval inside the eligible set. Only then run ordinary ranking.

Metadata filters are deterministic structural restrictions. ANN remains approximate semantic ranking within eligible candidates; filters do not make ANN membership deterministic. The accepted ANN contract remains `APPROXIMATE_MEMBERSHIP`.

Filters affect discovery candidates only. They do not alter PROV-O closure or `coverage.complete`; after a valid seed exists, the unchanged documentary scope policy applies. `DocumentAssociation` remains a separate bounded operation for neighbors of known documents and must not be merged with metadata filtering.

A future non-LLM parser fixture may map “PDF de mars 2024” to exact PDF type plus `production_month=2024-03`, but only after calendar basis and production-source policy are explicit or clarified. Natural-language planning is out of scope here.

## Z. LLM cost

metadata filtering LLM calls: 0

association construction LLM calls: 0

## AA. Scope/coverage impact

scope traversal changed: no

coverage contract changed: no

`openrag.scope-coverage v1` remains valid. No association expanded scope.

## AB. Corpus impact

production writes: 0

mapping changes: 0

Corpus digest and document/occurrence/chunk/embedding counts remained unchanged; OpenSearch stayed green.

## AC. Tests

- Association corpus audit, bucket/cardinality, mega-hub, deterministic cohort, pair-bound and artifact gates: PASS.
- DLS neighborhood tests: PASS, including five controlled corpus partitions.
- Three-valued logic, negation, all seven operators, date/timezone, multi-observation conflict, type/source/creator, exact normalization and canonical serialization: PASS.
- Metadata/backfill and association targeted suite: 110 passed.
- Full unit suite: 1,762 passed, 94 pre-existing warnings.
- Ruff changed files: PASS.
- Mypy changed source/scripts/tests: PASS.
- `uv lock --check`: PASS.
- `git diff --check`: PASS.
- GT1/GT2 full campaign: not required because product retrieval behavior did not change; frozen qrels were untouched.

## AD. Association readiness

DOCUMENT_ASSOCIATION_NEIGHBORHOOD:
BLOCKED

Reason: the current v1 policy conflates explicit `attachment_of` parents with broad `member_of`/`contained_in` collections under a STRONG dimension. The 30,901-occurrence hub makes every N1/N2 result STRONG and saturates all bounds. Source-family keys are also unpopulated by v1 despite available provenance. Bounds and DLS are correct, but quality-safe experimental activation requires a separate future policy change and human review.

## AE. Metadata filter readiness

METADATA_FILTERS:
PARTIAL

The versioned pure semantics, fail-closed logic, real cardinalities and tests are ready. Efficient candidate restriction is not: raw profiles are non-queryable (`enabled:false`), representative-only, and application-side full scanning is too slow. A normalized, source-qualified, DLS-safe projection and occurrence availability-state design must be reviewed and backfilled before experimental activation.

## AF. Recommended activation order

metadata filters first

Justification: their semantics are validated and their remaining work is a bounded indexing/integration problem. Association neighborhood has a direct quality blocker in the current strength policy. Do not attempt controlled joint activation until metadata filtering is independently safe and the parent-role association policy is corrected and reviewed.

## AG. Remaining blockers

- Split explicit-parent evidence from broad collection membership in a future association-policy version; remeasure and human-review neighborhoods before activation.
- Populate source-family association keys only through an explicit, reviewed policy; do not infer genre or expand scope.
- Design and review normalized source-qualified OpenSearch fields on searchable chunks (or prove an equivalent side-index design), then run a no-mutation canary/reindex plan in a separate chantier.
- Persist/reconcile metadata availability at occurrence and selected-record granularity so the historical 270 `UNKNOWN`/`UNAVAILABLE` records remain fail-closed despite two alternate pre-existing profiles.
- Complete human judgments in the separate 480-row review artifact; do not convert them to GT1/GT2 qrels.
- Integrate structured filters into retrieval only in a later authorized chantier and re-run functional retrieval/DLS/GT gates then.

## AH. Product changes

retrieval defaults changed: no

OpenSearch mapping changed: no

public API changed: no

deployment: no

GitOps: no

## AI. Qwen

QWEN_READINESS:
BLOCKED

## AJ. Conclusion

POST-BACKFILL METADATA/ASSOCIATION AUDIT PARTIAL
