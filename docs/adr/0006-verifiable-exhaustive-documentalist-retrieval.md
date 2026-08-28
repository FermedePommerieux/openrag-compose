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

OpenRAG exposes one model-facing retrieval operation backed by two server-side
primitives. This boundary is deliberate: ordinary archive search is always
available, while complete source-order reading cannot be selected from vague
topic wording.

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
identified document explicitly selected by the human. Words such as
“exhaustive”, “complete” or “all” in an archive topic do not activate it. They
trigger normal search immediately and never a confirmation round-trip. This
prevents an LLM from expanding one prompt into an unbounded sequence of costly
document reads.

Once a selected-document read starts, the agent follows `next_cursor` in the
same turn until `complete=true`. An incomplete, inaccessible, changed or
unverifiable document is reported as incomplete. An API client may deliberately
read several named documents, but coverage is then the conjunction of those
independent reads; complete reading of one document says nothing about files
excluded by ranked discovery.

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
cursor and a full pagination run. Model evaluation records retrieval coverage
separately from answer correctness so a capable model cannot conceal a
retrieval omission.
