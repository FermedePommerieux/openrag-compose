# ADR 0006 — Verifiable exhaustive documentalist retrieval

Status: accepted

Related research: [Verifiable documentalist RAG: state of the art and design
position](../research/verifiable-documentalist-rag-state-of-the-art.md)

## Context

OpenRAG is required to behave as an exhaustive digital documentalist. Its
primary objective is truthful, reproducible use of the indexed corpus. Latency,
token use and index size are secondary. A fixed semantic `top-k` cannot prove
that a fact is absent, enumerate every occurrence, compare several long files,
or establish that every source unit was inspected.

Retrieval summaries and stronger language models improve navigation and
reasoning, but neither is primary evidence. They can omit details or introduce
unsupported statements. The system therefore needs an observable distinction
between relevant evidence discovery and complete corpus reading.

## Decision

OpenRAG exposes one model-facing retrieval operation backed by three distinct
server-side paths: ranked discovery, complete reading of one selected document,
and exhaustive investigation of a query-defined documentary scope. Keeping
the paths explicit prevents a top-k result from being presented as coverage.

### Normal archive search

Normal search combines lexical and vector lanes with reciprocal-rank fusion.
It first allocates the configured base quota to every represented document,
then lets large profiled documents contribute up to an adaptive square-root
quota. This is a relevance mechanism. It must never be described as exhaustive.

The ingestion profile determines the adaptive quota:

`min(document chunks, adaptive ceiling, max(base quota, ceil(sqrt(document chunks))))`

For example, with base `3` and ceiling `20`, documents containing 3, 100 and
400 chunks can contribute 3, 10 and 20 focused results respectively.

### Complete reading of a selected document

The backend can read every leaf chunk of one selected document in deterministic
source order. It uses `search_after` pagination and returns a coverage object
containing the immutable snapshot digest, filename, chunks covered, total
chunks, coverage ratio, continuation cursor and completion flag.

The chat tool exposes this primitive only as `read_document_id`, for one already
identified document explicitly selected by the human. Topic wording never
selects an arbitrary single document.

Once a selected-document read starts, the agent follows `next_cursor` in the
same turn until `complete=true`. An incomplete, inaccessible, changed or
unverifiable document is reported as incomplete. An API client may deliberately
read several named documents, but coverage is then the conjunction of those
independent reads; complete reading of one document says nothing about files
excluded by ranked discovery.

### Exhaustive investigation of a documentary scope

An explicit request for all exchanges, all related documents, or a complete
chronology can select `scope_exhaustive`. The server first reuses normal
Retrieval v2 to obtain broad lexical/vector/RRF seeds. It then closes the
accessible documentary graph from their PROV-O entities under the explicit
`documentary-prov-o` scope policy, version `1`:

- outgoing relations are read from each entity's canonical provenance object;
- incoming relations use a typed nested OpenSearch query, ensuring role,
  source type, target type and target id belong to the same policy rule;
- every OpenSearch request uses the current user's DLS-scoped client;
- visited identifiers, frontier and results are ordered deterministically;
- cycles terminate and forward/reverse traversal continues to an empty
  frontier or an explicit safety bound.

The PROV-O provenance graph is not the documentary scope graph. Policy v1
classifies every visible typed relation as `scope-defining`, `contextual`,
`identity-only`, `infrastructure`, or unclassified. Its initial matrix is:

| Role | Source type | Target type | Forward | Reverse | Transitive | Semantics |
| --- | --- | --- | --- | --- | --- | --- |
| `attachment_of` | `email_attachment` | `email_message` | yes | yes | controlled | scope-defining |
| `member_of` | `email_message`, `email_attachment` | `email_thread` | yes | yes | yes | scope-defining |
| `reply_to` | `email_message` | `email_message`, `email_message_identifier` | yes | yes | yes | scope-defining |
| `references` | `email_message` | `email_message`, `email_message_identifier` | yes | yes | yes | scope-defining |
| `contained_in` | `email_message`, `email_attachment` | `email_archive` | no | no | no | contextual |
| `member_of` | `file` | `directory_collection` | no | no | no | infrastructure |

For example, `email → contained_in → email_archive` remains provenance context
and is returned in compact context metadata, but the archive is never reverse
expanded. Conversely, `email → member_of → email_thread` is scope-defining and
reverse expansion reconstructs the complete accessible thread. Classification
happens before reverse search, so excluded archive and ingestion-root hubs are
not scanned and discarded afterward.

Every discovered document is then read by repeatedly calling the existing
selected-document primitive. Ranked seed chunks are discovery evidence, never
proof that the document was read completely.

The certificate exposes `scope_policy_id` and `scope_policy_version`, plus
compact counters for relations traversed, retained as context, excluded by the
policy, and unclassified. Scope coverage is complete only when seed discovery found at least one
PROV-O-identifiable document, every seed can participate in provenance closure,
graph traversal ended with an empty frontier without reaching a bound, and
every discovered document completed its verified immutable-snapshot read. A legacy profile,
snapshot change, cursor failure, inaccessible read or any graph/document limit
makes coverage incomplete with an explicit stop reason. A known non-expansive
relation is compatible with completion. An unknown role/type triple is not: it
produces `scope_policy_unclassified_relation` and prevents certification.

#### Coverage certification invariants

`complete=true` certifies only the accessible, indexed, provenance-connected
scope reached from the ranked seeds. It never certifies the whole physical
archive, documents not indexed, or documents hidden by DLS. The value is
produced by one fail-closed decision function and requires all of these facts:

1. ranked seed discovery completed without a search error;
2. at least one seed document has valid canonical `source_provenance`;
3. every seed document has valid provenance, including agreement between the
   canonical object and denormalized identity fields;
4. forward and nested reverse traversal ended at a natural empty frontier under
   the declared versioned `ScopeTraversalPolicy`;
5. no depth, entity, document, result-window or identity-ambiguity guard was
   encountered;
6. every DLS-visible discovered document was read in contiguous source order;
7. every chunk digest, snapshot binding, cursor and exact counter was valid;
8. the canonical whole-document SHA-256 recomputed from all returned chunks
   matched the immutable ingestion snapshot.

Reaching a limit exactly is not an error when the next visible frontier is
empty. A limit is reported only when an additional accessible entity or
document would cross it. Conversely, an invisible relation target is absent
from `documents`, `entities` and `edges`, consumes no limit and cannot prevent
natural closure. Active public filters are included in every graph query.

The certificate exposes stable `status_code`, `status_message` and ordered
`failure_codes` fields. Current incomplete codes are
`incomplete_seed_discovery`, `search_error`, `no_provenance_seed`,
`seed_missing_provenance`, `graph_limit_reached`,
`graph_traversal_failed`, `scope_policy_unclassified_relation`,
`document_limit_reached`,
`document_read_incomplete`, `legacy_document`, `snapshot_changed`,
`cursor_invalid`, `access_error`, `profile_invalid` and
`identity_ambiguous`. Partial verified chunks and per-document statuses remain
in the response when another read fails. `coverage_ratio` and
`document_read_coverage_ratio` measure leaf-reading progress only; even a value
of `1.0` is not proof of graph closure or valid seed discovery.

Alternate identifiers are an identity layer, not an independent scope edge.
They resolve targets of policy-approved message relations and may support
incoming relation lookup, but never merge primary owners by themselves. A
shared RFC 5322 Message-ID is accepted as several legitimate occurrences only
when all owners are typed messages with the same timestamp and subject and each
belongs to a distinct explicit source container. The primary entities remain
separate. Missing or conflicting evidence stays `identity_ambiguous`.

The full response artifact retains every verified leaf chunk for provenance and
the UI. To keep one investigation from blindly filling the LLM context with
hundreds of documents, the model-facing projection contains ranked leaf seeds
plus at most one source-order leaf from newly linked documents, a document
manifest and the coverage certificate. These remain real citable leaf chunks;
generated summaries never replace evidence.

## Ingestion analysis and snapshot identity

The final ingestion callback performs a deterministic analysis pass after all
leaf chunks are indexed and refreshed. It records on every chunk:

- leaf chunk count;
- distinct page count and maximum page number;
- total indexed character count;
- size class;
- SHA-256 of the complete ordered leaf set.

Each leaf also stores its own text SHA-256. The final analysis pass scans every
leaf in source order; it does not use probabilistic cardinality aggregations.
It recalculates each text SHA-256 and counts pages and characters exactly. The
document snapshot SHA-256 covers every stable logical chunk id, verified text
digest, global source-order index and page reference. Finalization fails if the
number of hashed chunks differs from the exact indexed count, if a text digest
or character count differs, or if source-order
indices are not unique and contiguous from `0` to `N-1`. An exhaustive cursor is
bound to `document_id`, the snapshot SHA-256, the authenticated principal and
the active filters. It is authenticated with a server-side HMAC, so a client
cannot edit its position or covered-chunk counter to manufacture completeness.
This also prevents pages from two replacement generations or access scopes from
being silently combined.

This profile is factual metadata, not an LLM-generated summary. Future page,
section or recursive summaries may be stored as navigation nodes, but factual
citations must resolve to leaf chunks containing the source text.

## Citation transport contract

Streaming Langflow events carry retrieval results as a native structured
artifact and the backend promotes that artifact to frontend tool results. The
non-streaming Langflow response can omit those tool-call artifacts even when
the assistant message contains valid `(Source: chunk_id)` citations. In that
case OpenRAG extracts identifiers only and hydrates every source card by exact
`chunk_id` through the requesting user's DLS-scoped OpenSearch client.

Filenames, model-produced metadata and Python object representations are never
treated as provenance. An inaccessible, malformed or missing id produces no
source card. This makes the model citation a reference request, while the
authenticated index remains the sole authority for filename, text, page,
document id, source URL and chunk metadata.

The Langflow tool sends two representations of the same authenticated result.
Its native artifact keeps every source field required by UI cards and citation
hydration. Its model-facing JSON keeps leaf text, citation and ordering fields,
plus one compact manifest entry per document. Repeated source URLs, ACL data and
full PROV-O JSON are not copied into every model-visible chunk. This reduces
token use without removing any evidence or source navigation from the user.

Document ids and cursors remain machine coordinates. Human-facing coverage is
labelled by the authenticated filename or source title, never a raw id such as
`EksI7_kmm2p9LEP7ki74nw_z`.

## Truth contract

OpenRAG distinguishes these outcomes:

- **supported**: the answer is backed by exact cited leaf chunks;
- **not found under complete coverage**: all in-scope source chunks were read
  and no matching evidence was found;
- **not retrieved**: ranked search found no supporting evidence, without a
  claim about absence from the corpus;
- **coverage incomplete**: the system cannot safely conclude and says why;
- **contradiction**: sources disagree; the answer reports every conflicting
  statement with its provenance rather than selecting one silently.

OCR confidence, parser warnings, duplicate/version relationships and section
maps should be added to the same evidence ledger as they become available.

## Operational consequences

- Existing documents without profile version 1 must be reindexed before they
  can use complete selected-document reading.
- New OpenAI ingestion defaults to `text-embedding-3-large`. Existing chunks
  retain their recorded embedding model until they are explicitly reindexed;
  vectors from different models are never relabelled as interchangeable.
- `retrieval_max_chunks_per_document` is the normal-search base diversity
  quota, not a document-wide limit.
- `retrieval_adaptive_max_chunks_per_document` is only the normal-search
  ceiling. It never truncates exhaustive reads.
- `retrieval_scope_seed_count`, `retrieval_scope_max_depth`,
  `retrieval_scope_max_entities`, `retrieval_scope_max_documents` and
  `retrieval_scope_batch_size` bound dossier investigations. Reaching a bound
  is reported as incomplete, never as successful coverage.
- The database retains every leaf chunk. No summary replaces source evidence.
- Langflow remains an orchestration client. The backend owns pagination,
  snapshot validation, ACL-scoped access and coverage accounting.
- Flow migration fingerprints ignore only provider fields owned by OpenRAG's
  settings synchronization (`model` and provider credential/endpoint
  references). Those fields are copied into the replacement graph rather than
  reset to bundled defaults. Prompt, code, nodes, edges and all other values
  remain covered, so selecting a model does not break a safe upgrade and a
  customized graph is still rejected.
- All backend replicas must share the same production `SESSION_SECRET`; it
  authenticates continuation cursors. A secret rotation invalidates active
  cursors and forces a safe restart of the evidence read.
- The ingest callback token remains a secret-typed Langflow global. The run id
  remains a non-secret string.
- A production deployment injects the repository slug, published branch and
  immutable revision of its installed flow files. The OpenRAG update prompt
  displays and links to that provenance. Its action replaces only the four
  OpenRAG flow definitions from `OPENRAG_FLOWS_PATH`; it does not upgrade the
  Langflow application or pull anything from an implicit upstream repository.
  Missing or malformed provenance is never replaced by an invented default.

## Validation requirements

Release validation must include small and long documents, multiple documents,
exact identifiers, paraphrases, contradictions, a changed snapshot, an invalid
cursor, forward/reverse provenance chains, cycles, DLS-hidden neighbours and a
full pagination run. Model evaluation records retrieval coverage
separately from answer correctness so a capable model cannot conceal a
retrieval omission.

Even complete graph closure proves only the accessible entities currently
indexed. It cannot prove that a source was never created, never ingested, or is
not hidden by the caller's permissions. User-facing claims must retain that
epistemic boundary.
