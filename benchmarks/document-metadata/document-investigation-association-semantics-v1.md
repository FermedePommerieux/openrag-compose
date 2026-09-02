# Document Investigation and Association Semantics v1

Status: offline design and pure implementation only

Policy: `openrag.document-investigation-association` v1

Input contracts: `openrag.document-metadata` v1 and `openrag.metadata-resolution` v1

## Scope

This design turns existing source-qualified metadata observations into auditable temporal
relations, deterministic documentary associations, and bounded investigative neighborhoods.
It adds no preferred metadata truth and no production integration.

The implementation consists of pure models and functions:

- `inspect_document_metadata`: canonicalizes observations for investigation, reports conflicts,
  derives source-qualified temporal views, retains safe provenance, and builds opaque candidate
  keys.
- `build_document_chronology`: preserves comparable UTC-instant relations, indeterminate
  relations, calendar-period comparisons, conflicts, and observation provenance for one source
  occurrence.
- `compare_document_metadata`: compares two distinct source occurrences and reports all matched
  association dimensions and their evidence.
- `build_document_association`: applies deterministic strength and status policy to a comparison.
- `build_documentary_neighborhood`: uses DLS-filtered, bounded metadata-key buckets to build a
  seed-centric neighborhood without an all-pairs scan.
- Evidence projection functions produce stable, bounded explanations only after an explicit
  accessible-document check.

The document identity used by investigation and association is
`DocumentMetadataProfile.entity_id`: the existing source entity/occurrence identity. A binary
SHA-256 is evidence about content identity, not a reason to collapse two occurrences.

## Non-goals

This chantier does not implement or change:

- retrieval, ranking, BM25, dense KNN, RRF, seed selection, or metadata filters;
- OpenSearch mappings, indices, scripts, reads, writes, or reindexing;
- connector behavior, metadata extraction, the historical backfill, or its checkpoints;
- public endpoints, agent tools, model prompts, GitOps, images, or deployments;
- embeddings, semantic similarity, fuzzy identity, an LLM classifier, or learned weights;
- PROV-O lineage edges, scope traversal, `openrag.scope-coverage` v1, or coverage certification.

## Evidence-state semantics

The model keeps the following distinctions explicit:

| State | Meaning |
|---|---|
| `ASSERTED` | A source-native relation was explicitly supplied, such as each attachment's parent. |
| `OBSERVED` | A value exists in a named metadata source; it is not declared true. |
| `ASSOCIATED` | Deterministic evidence values overlap under policy v1. |
| `INFERRED` | Reserved for a future explicitly labeled inference; v1 creates none. |
| `UNKNOWN` | Evidence is absent or cannot support the requested comparison. |
| `CONFLICTING` | Matching evidence coexists with different source-qualified observations. |
| `INVALID` | A raw value is retained but cannot be normalized. |

An observation is not an asserted relation. An asserted relation is not an association. An
association is not lineage, inference, alternate identity, causality, attribution, or truth.
Trust class describes origin and never selects truth.

## Observation model

`inspect_document_metadata` consumes `DocumentMetadataProfile` without altering it. All metadata
observations are returned in canonical fact order. Extraction wall-clock time is retained for
audit but excluded from observation and association identities.

The inspection contains:

- original v1 observations;
- `DocumentTemporalObservation` values;
- enriched `InvestigationMetadataConflict` values;
- safe source provenance with entity type, source system, and asserted relation identities, but
  no URL, label, path, archive locator, or attachment secret;
- association-ready normalized values;
- opaque SHA-256 candidate keys.

The original profile's unresolved conflicts remain unresolved. No preferred creation date,
creator, filename, source, MIME, or type is materialized.

## Temporal model

Each `DocumentTemporalObservation` retains:

- semantic role and original normalized field;
- raw value and normalized value;
- timezone state and original timezone label;
- source, source type, and trust class;
- observation status, extraction time, and normalization version;
- normalized UTC instant when and only when an offset is explicit;
- source-local and UTC day, month, and year when determinable.

The v1 field-to-role mapping is explicit:

| Metadata field | Temporal role |
|---|---|
| `embedded_created_at`, `embedded_sent_at` | `PRODUCTION` |
| `embedded_modified_at` | `MODIFICATION` |
| `embedded_digitized_at` | `DIGITIZATION` |
| `filesystem_birthtime` | `FILESYSTEM_BIRTHTIME` |
| `filesystem_mtime` | `FILESYSTEM_MODIFICATION` |
| `filesystem_ctime` | `FILESYSTEM_CHANGE` |
| `archived_at` | `ARCHIVED` |
| `archive_created_at` | `ARCHIVE_CREATION` |
| `archive_modified_at` | `ARCHIVE_MODIFICATION` |
| `ingested_at` | `INGESTION` |

Those roles are not silently merged. In particular, a filesystem timestamp is not treated as an
embedded document modification time, and an archive timestamp is not treated as a creation time.

Instant comparison is deterministic and non-LLM:

- two offset-aware observations compare as `BEFORE`, `AFTER`, or `EQUAL` in UTC;
- if either offset is unknown or either timestamp is invalid, the relation is `INDETERMINATE`.

`DocumentChronology` is a partial-order representation. It has no canonical creation date and
retains comparable and indeterminate relations separately.

## Calendar-period semantics

Calendar period and instant semantics are separate.

### Source-local calendar

The unsuffixed association dimensions use the calendar components in each source observation:

- `SAME_PRODUCTION_DAY`, `SAME_PRODUCTION_MONTH`, `SAME_PRODUCTION_YEAR`;
- `SAME_MODIFICATION_DAY`, `SAME_MODIFICATION_MONTH`, `SAME_MODIFICATION_YEAR`.

A timezone-naive but syntactically valid timestamp can support a source-local calendar comparison.
It cannot support an instant or UTC-calendar comparison.

### UTC calendar

Offset-aware observations additionally support explicitly suffixed dimensions:

- `SAME_PRODUCTION_DAY_UTC`, `SAME_PRODUCTION_MONTH_UTC`,
  `SAME_PRODUCTION_YEAR_UTC`;
- `SAME_MODIFICATION_DAY_UTC`, `SAME_MODIFICATION_MONTH_UTC`,
  `SAME_MODIFICATION_YEAR_UTC`.

Exact comparable instants are reported separately as `SAME_PRODUCTION_INSTANT` or
`SAME_MODIFICATION_INSTANT`.

For example, `2024-03-31T23:30:00Z` and `2024-04-01T01:30:00+02:00` are the same instant and
the same UTC month, but different source-local months. The comparison exposes all three outcomes;
it does not choose one calendar silently.

### Timezone states

| State | v1 behavior |
|---|---|
| `EXPLICIT_OFFSET` | UTC instant and both calendar bases are available. |
| `ASSUMED_BY_FORMAT` | Representable for future explicit parser policies; never invented by the current adapter. |
| `UNKNOWN` | Source-local calendar only; instant and UTC calendar are indeterminate. |
| `INVALID` | Raw value retained; no instant or calendar period is emitted. |

## Conflict model

Investigation reports, without resolving:

- `TIMEZONE_UNKNOWN`;
- `INVALID_TIMESTAMP`;
- `SOURCE_CONFLICT`;
- `MULTIPLE_CREATION_OBSERVATIONS`;
- `MULTIPLE_MODIFICATION_OBSERVATIONS`;
- `CREATOR_CHANGED`;
- `MODIFIED_BEFORE_CREATED`;
- `ARCHIVE_EMBEDDED_DATE_INVERSION`;
- `FUTURE_TIMESTAMP`.

If one source-qualified value matches another document while additional values disagree, the
dimension remains visible with status `CONFLICTING`. It is not converted into a preferred match.

## DocumentAssociation model

`DocumentAssociation` contains canonical left and right occurrence identities, dimensions,
dimension results, evidence, strength, status, policy id/version, the mandatory
`scope_expanding=false`, and a canonical SHA-256 digest.

Every evidence item contains:

- dimension and evidence state;
- both stable observation identities;
- both normalized fields and sources;
- the internal comparison value;
- calendar basis and normalized period when temporal.

`document_association_sha256` covers endpoints, dimensions, evidence, strength, status, and policy
version. It excludes extraction wall-clock time. Endpoints, dimensions, evidence, and JSON keys are
canonically ordered, so comparing A/B or B/A yields identical canonical JSON and digest for these
symmetric v1 dimensions.

## Association dimensions v1

Implemented dimensions are:

- production: exact instant, source-local day/month/year, UTC day/month/year;
- modification: exact instant, source-local day/month/year, UTC day/month/year;
- source: `SAME_SOURCE_SYSTEM`, `SAME_SOURCE_ENTITY_FAMILY`,
  `SAME_PARENT_COLLECTION`;
- type/format: `SAME_DOCUMENT_TYPE`, `COMPATIBLE_DOCUMENT_TYPES`, `SAME_MIME_TYPE`,
  `SAME_EXTENSION`;
- observed actors/tools: `SAME_CREATOR_OBSERVATION`,
  `SAME_LAST_MODIFIER_OBSERVATION`, `SAME_PRODUCER_OBSERVATION`,
  `SAME_CREATOR_APPLICATION_OBSERVATION`;
- filename: `SAME_FILENAME_BASENAME`;
- binary identity: `SAME_BINARY_HASH`.

Dimension evidence is a vector; no opaque scalar score or learned ranker exists.

## Source semantics

Source levels remain distinct:

- source system is an exact normalized `SourceProvenance.entity.source_system` or observed
  `archive_source` value;
- source entity family is emitted only from an explicit `source_entity_family` or
  `source_occurrence_family` observation;
- shared parent collection is emitted when both occurrences have their own explicit
  `attachment_of`, `member_of`, or `contained_in` assertion to the same target identity.

Two OpenArchiver items with different parent emails therefore share only the broad source-system
dimension. Two attachments of one email retain two separate asserted `attachment_of` relations;
the pairwise shared-parent fact is a `STRONG` association, not a new PROV-O edge.

## Type semantics

Technical MIME, format family, and documentary type are different values:

- `SAME_MIME_TYPE` requires exact normalized technical MIME observations;
- `COMPATIBLE_DOCUMENT_TYPES` uses a deterministic MIME-derived technical family such as
  text document, spreadsheet, presentation, image, email, PDF, or plain text;
- `SAME_DOCUMENT_TYPE` requires the same explicit `documentary_type` or
  `source_declared_type` observation.

Extension alone never invents a format family or documentary genre. No content or LLM classifier
is used. For example, DOCX and ODT can be compatible text-document formats without being the same
MIME or an observed documentary genre such as "invoice".

## Creator and producer semantics

Creator/author, last modifier, producer, and creator application are compared as separate exact
normalized observations. Unicode normalization, whitespace normalization, and case-folding are
deterministic conveniences, not entity resolution. `Jean Dupont` and `J. Dupont` do not match.
Raw actor/source values are absent from candidate keys and bounded association projections.

## Hash, filename, and occurrence semantics

The same complete SHA-256 is a strong binary-identity signal. It does not merge occurrences,
owners, source relations, or document identities.

Filename basename equality is weak and non-expanding. `rapport.docx` and `rapport.pdf` can share a
basename; two unrelated `facture.pdf` occurrences can share basename and extension. Neither case
establishes lineage. Filenames never identify OpenArchiver attachments.

## Deterministic combination policy

Strength is an explainable class, not a score:

1. `NONE`: no dimension matched; association status is `UNKNOWN`.
2. `STRONG`: `SAME_BINARY_HASH` or `SAME_PARENT_COLLECTION` is present.
3. `MEDIUM`: either explicit same source-entity family combines with another evidence family, or
   at least three independent families match and at least one discriminating dimension is present.
   Evidence families are temporal, source, type, actor/tool, filename, and binary identity.
4. `VERY_WEAK`: every match is a mega-hub-prone dimension: year, broad source system, MIME,
   extension, or compatible technical format family.
5. `WEAK`: every other non-empty association, including month alone, basename alone, exact creator
   alone, or source plus month.

Discriminating dimensions include exact instants/days, source entity family, exact observed actors,
explicit document type, and basename. Thus source + production month + exact creator is `MEDIUM`,
while source + MIME + year remains `VERY_WEAK`. Conflicting dimension status remains visible and
sets the association status to `CONFLICTING`; no observation is dropped.

## Canonical ordering

Associations order by:

1. strength (`STRONG`, `MEDIUM`, `WEAK`, `VERY_WEAK`, `NONE`);
2. fixed dimension priority (binary hash and shared parent first; broad year dimensions last);
3. canonical occurrence identities;
4. canonical evidence identity.

Observation input order and bucket input order do not affect output.

## Non-expanding rule and coverage

`DocumentAssociation.scope_expanding` is a literal `false`; callers cannot construct `true`.
Candidate lineage also fixes `scope_expanding=false` and `prov_o_edges=0`.

No association dimension, strength combination, or neighborhood can expand the certifiable
PROV-O closure. The existing `ScopeTraversalPolicy` has no association rule and continues to fail
closed for an unknown `associated_with` relation. `openrag.scope-coverage` v1 is unchanged.

The neighborhood reports `BOUNDED_NOT_EXHAUSTIVE`; it does not expose `coverage.complete` and is
never called a scope closure.

## Bounded documentary neighborhood

The implemented offline primitive accepts seed occurrence identities, already-inspected profiles,
an explicit DLS-accessible identity set, selected dimensions, and limits:

- `max_documents`;
- `max_associations`;
- `per_dimension_limit`;
- optional UTC-instant `time_window_days`;
- optional exact normalized `source_scope`.

Each included non-seed document exposes the association digests and dimensions that caused its
inclusion. The output is stable and always non-expanding.

## Scale and mega-hub controls

The implementation does not evaluate corpus-wide all-pairs. Its future-compatible path is:

```text
metadata profile
→ normalized association-ready values
→ opaque dimension/value SHA-256 keys
→ DLS-filtered bounded candidate buckets
→ deterministic pair evidence
→ bounded seed-centric neighborhood
```

Candidate keys are domain-separated SHA-256 values. Creator, source, parent, filename, and binary
values never appear raw in a key. Each seed reads at most `per_dimension_limit` canonical candidate
identities across all of its buckets for one dimension. Global document and association limits
apply afterward. Broad year,
source-system, MIME, extension, and format-family buckets are therefore bounded and never become
scope-expanding mega-hubs.

No production index or OpenSearch mapping is created by this design.

## DLS model

DLS filtering occurs before bucket construction. Inaccessible documents therefore affect neither
candidate choice nor truncation signals. The neighborhood and projection functions disclose no
hidden identity, title, actor, source, relationship, or count.

Evidence projection requires both association endpoints to be accessible. A failed check returns
nothing. Metadata projection requires its document identity to be accessible. These pure checks
prepare the semantics; a future activation must bind the accessible set to the authenticated
product DLS decision and re-audit every projection surface.

## Evidence projection

`DocumentMetadataEvidenceProjection` limits temporal observations and conflicts.
`DocumentAssociationEvidenceProjection` limits dimensions, uses a fixed stable order, and describes
reasons without raw creator, source, parent, filename, or hash values. Example:

```text
Document B is associated with Document A because:
- same observed production month (source-local calendar): 2024-03
- same observed source system
- same exact observed creator value
```

It never says "version of". `CandidateLineageEvidence` is explicitly `candidate_only`,
non-expanding, and contains zero PROV-O edges.

## Synthetic and adversarial validation

Committed fixtures and pure unit tests cover:

- same month/year/source/format/creator independently and in combinations;
- same binary in distinct occurrences, basename collisions, unrelated dates, and distant years;
- same-parent attachments and unrelated emails from the same source system;
- timezone-aware versus naive values, equivalent instants with local month/year boundaries,
  invalid/future dates, modified-before-created, archive inversion, and PDF Info/XMP conflict;
- exact creator observation without global person resolution;
- cross-format compatibility without invented documentary type;
- year/source/MIME mega-hub bounds and non-expansion;
- DLS-hostile hidden records filtered before bucketing and projection;
- metadata-order, A/B direction, canonical JSON, extraction-time, and bucket-order determinism;
- no production/runtime dependency and no domain-specific product logic.

## Future activation path

Activation is deliberately deferred until after the running full metadata backfill and a separate
review. Candidate work, in order of increasing product impact, is:

1. document metadata inspection;
2. chronology query;
3. document association neighborhood;
4. metadata filters;
5. metadata-aware retrieval;
6. lineage inference;
7. PROV-O enrichment.

Each step requires its own DLS, privacy, scale, public contract, rollout, and regression review.
Nothing in this branch enables any step.
