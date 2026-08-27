# Verifiable documentalist RAG: state of the art and design position

Last reviewed: 2026-08-26

## Research objective

OpenRAG is not optimized primarily for a plausible answer at minimum latency.
The target is a digital documentalist: discover relevant sources, read the
complete stated scope when the question requires it, preserve conflicting
evidence, and make the difference between retrieval and proof observable.

This note uses public primary or vendor-authored sources. It does not infer a
private product architecture from user-visible behavior.

## What is publicly known about ChatGPT projects

OpenAI documents that ChatGPT Projects can use uploaded files, project chats
and project instructions as context. The public Projects documentation does
not specify the internal parser, chunking algorithm, embedding model, index
layout, retrieval depth, or a completeness certificate. Consequently, it is
not defensible to claim that ChatGPT Projects uses a particular chunk size or
indexing architecture.

The public OpenAI Vector Store API is a useful product comparison, but it is
not evidence of ChatGPT Projects internals. Its search endpoint returns
relevance-ranked chunks, supports file-attribute filters, optional query
rewriting and ranking options, returns at most 50 results per call, and exposes
result pagination. That is a strong discovery interface; its public response
does not establish that every leaf in an identified document was read.

Sources:

- [Projects in ChatGPT](https://help.openai.com/en/articles/10169521-projects-in-chatgpt)
- [OpenAI Vector Store search API](https://developers.openai.com/api/reference/python/resources/vector_stores/methods/search)

## Relevant advanced RAG families

| Work | Main contribution | Value for OpenRAG | Limit relative to the documentalist target |
|---|---|---|---|
| [RAPTOR](https://arxiv.org/abs/2401.18059) | Recursive clustering and summaries form a multi-level retrieval tree. | Strong navigation and holistic context for long sources. | Generated summaries can omit or distort details; they cannot be the evidence ledger. |
| [LongRAG](https://arxiv.org/abs/2406.15319) | Larger retrieval units and a long-context reader reduce fragmentation and retrieve few long units. | Encourages document/section-scale context rather than isolated fragments. | A small top-k of long units improves recall but does not prove complete source coverage. |
| [ColBERTv2](https://arxiv.org/abs/2112.01488) | Token-level late interaction improves fine-grained retrieval with compressed representations. | Candidate for higher-recall focused discovery and reranking. | It remains a ranking mechanism; unreturned evidence is not accounted for. |
| [Late Chunking](https://arxiv.org/abs/2409.04701) | Embeds long context before pooling chunk representations. | Preserves surrounding context in leaf embeddings. | Better embeddings do not certify that every leaf was inspected. |
| [GraphRAG](https://www.microsoft.com/en-us/research/project/graphrag/overview/) | Entity graphs, communities and reports support corpus-level questions. | Useful cross-document map and global navigation layer. | LLM-derived graphs and reports are secondary representations and may omit leaf facts. |
| [DRIFT Search](https://www.microsoft.com/en-us/research/blog/introducing-drift-search-combining-global-and-local-search-methods-to-improve-quality-and-efficiency/) | Combines global and local search through iterative graph traversal. | A promising planner for moving from corpus themes to exact source passages. | Traversal quality is not itself an auditable statement that the full scope was read. |
| [RAGChecker](https://arxiv.org/abs/2408.08067) | Separates retrieval and generation diagnostics with fine-grained metrics. | Supports evaluation that cannot hide a retrieval miss behind a capable model. | Evaluation metrics diagnose a run; they do not create an operational coverage guarantee. |

## Design synthesis

The strongest architecture is multi-resolution, but the layers have different
epistemic roles:

1. lexical/vector/late-interaction retrieval discovers candidates;
2. section, recursive or graph summaries navigate long and cross-document
   corpora;
3. immutable leaf chunks remain the only citable source evidence;
4. an exhaustive reader accounts for every leaf in each declared document;
5. the generator reasons only over evidence whose provenance and coverage are
   visible.

OpenRAG currently implements layers 1, 3, 4 and 5. Hierarchical summaries,
late-interaction retrieval and graph navigation are future candidates for layer
2; none may replace the leaf ledger.

## Distinctive contribution of this project

The claim is not that hashing, cursor pagination or hybrid retrieval is new in
isolation. The project-specific contribution is their composition into an
agent-visible truth contract:

- ingestion creates an exact, ordered, content-verified leaf snapshot;
- focused retrieval is explicitly labelled non-exhaustive and adapts its quota
  from a factual document profile;
- exhaustive retrieval has no semantic top-k cutoff;
- every continuation cursor is authenticated and bound to document snapshot,
  principal and filters;
- the response carries cumulative coverage and cannot report completion after
  an altered cursor, mixed generation, missing leaf or changed access scope;
- multi-document completeness is the conjunction of each completed document
  read;
- the prompt forbids universal or absence claims without complete coverage;
- evaluation records retrieval coverage separately from answer correctness.

This creates a falsifiable property that ordinary RAG answers usually lack:
for a stated document scope, the system can show whether all indexed source
leaves were actually traversed. It still cannot claim that OCR or parsing
captured content that never entered the index. Parser/OCR confidence, original
file checksums, layout regions and extraction warnings must therefore become
the next layer of the evidence ledger.

## Evaluation implications

The model matrix (GPT-5.5 and GPT-5.6 Luna, Terra and Sol) must use the same
retrieval snapshots and record at least:

- document selection recall;
- leaf coverage completion per document;
- exact citation support and citation correctness;
- contradiction retention;
- abstention when coverage is incomplete;
- answer correctness after complete evidence;
- parser/OCR ground-truth errors separately from retrieval and generation.

A stronger model may improve synthesis. It must never turn incomplete
retrieval into a reported success.

## Execution durability and economic verifiability

Truth-oriented retrieval cannot bind a long audit's lifetime to an HTTP
subscriber. The archive audit therefore runs as an owner-scoped durable job:
the live stream is only one viewer, progress and the terminal result are stored
in OpenRAG's persistent backend database, and a disconnected chat recovers the
same job rather than silently launching a second paid search.

Economic claims follow the same evidence principle. The UI reports provider
token counts and a dated price-derived cost for every answer. Exhaustive audits
sum all constituent calls—query embeddings, bounded evidence readers,
coordinators, validators and the final agent response. Unknown pricing is
reported as unknown; it is never inferred. See
[ADR 0007](../adr/0007-durable-chat-audits-and-cost-ledger.md).

Semantic exhaustiveness is not claimed from a nearest-neighbour engine. A weak
vector tail is always available and its scores are similarities, not calibrated
probabilities. Archive audits therefore exhaust precise lexical predicates and
verified provenance relations, but bound semantic discovery to the production-
calibrated useful neighbourhood. See
[ADR 0008](../adr/0008-relevance-bounded-archive-audits.md).

Once OpenSearch admits a document to that neighbourhood, no excerpt-level LLM
label may remove it. The audit reads the full candidate set, constructs a
lossless answer-claim plan, and validates each final claim directly against
only its cited original chunk segments. This preserves the correct burden of
proof while bounding model context and tokens; see
[ADR 0009](../adr/0009-answer-claims-verified-against-sources.md).
