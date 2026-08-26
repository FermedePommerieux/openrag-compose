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

OpenRAG has two evidence modes.

### Focused discovery

Focused mode combines lexical and vector lanes with reciprocal-rank fusion.
It first allocates the configured base quota to every represented document,
then lets large profiled documents contribute up to an adaptive square-root
quota. This is a relevance mechanism. It must never be described as exhaustive.

The ingestion profile determines the adaptive quota:

`min(document chunks, adaptive ceiling, max(base quota, ceil(sqrt(document chunks))))`

For example, with base `3` and ceiling `20`, documents containing 3, 100 and
400 chunks can contribute 3, 10 and 20 focused results respectively.

### Exhaustive evidence reading

Exhaustive mode reads every leaf chunk of one selected document in deterministic
source order. It uses `search_after` pagination and returns a coverage object
containing the immutable snapshot digest, chunks covered, total chunks,
coverage ratio, continuation cursor and completion flag.

An agent must follow `next_cursor` until `complete=true` for every document in
scope before claiming that a result is complete. For a multi-document request,
coverage is the conjunction of the independently completed document reads.
If any read is incomplete, inaccessible, changes version, or contains an
unverifiable legacy chunk, the answer must disclose incomplete coverage and
must not use universal claims such as “all”, “none” or “exhaustive”.

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

## Truth contract

OpenRAG distinguishes these outcomes:

- **supported**: the answer is backed by exact cited leaf chunks;
- **not found under complete coverage**: all in-scope source chunks were read
  and no matching evidence was found;
- **not retrieved**: focused search found no supporting evidence, without a
  claim about absence from the corpus;
- **coverage incomplete**: the system cannot safely conclude and says why;
- **contradiction**: sources disagree; the answer reports every conflicting
  statement with its provenance rather than selecting one silently.

OCR confidence, parser warnings, duplicate/version relationships and section
maps should be added to the same evidence ledger as they become available.

## Operational consequences

- Existing documents without profile version 1 must be reindexed before they
  can use exhaustive mode.
- New OpenAI ingestion defaults to `text-embedding-3-large`. Existing chunks
  retain their recorded embedding model until they are explicitly reindexed;
  vectors from different models are never relabelled as interchangeable.
- `retrieval_max_chunks_per_document` is the focused-search base diversity
  quota, not a document-wide limit.
- `retrieval_adaptive_max_chunks_per_document` is only the focused-search
  ceiling. It never truncates exhaustive reads.
- The database retains every leaf chunk. No summary replaces source evidence.
- Langflow remains an orchestration client. The backend owns pagination,
  snapshot validation, ACL-scoped access and coverage accounting.
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
