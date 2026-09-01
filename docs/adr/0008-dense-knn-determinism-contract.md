# ADR 0008: Dense KNN determinism is conditional on its exact input

## Status

Accepted for Retrieval v2.

## Context

OpenRAG uses OpenSearch 3.6 JVector `disk_ann` with L2 distance. The engine is
an approximate nearest-neighbor implementation. The deployed plugin performs a
PQ graph search and exact reranking of a bounded candidate pool; its default
`overquery_factor` is 5. For the product dense horizon `k=50`, that means an
internal exact rerank over as many as 250 ANN candidates.

Official references:

- [OpenSearch vector search techniques](https://docs.opensearch.org/latest/vector-search/vector-search-techniques/index/)
  distinguishes approximate nearest-neighbor search from brute-force exact
  search.
- [OpenSearch k-NN query](https://docs.opensearch.org/latest/query-dsl/specialized/k-nn/index/)
  documents candidate/search parameters and on-disk rescoring.
- [OpenSearch exact k-NN scoring](https://docs.opensearch.org/latest/vector-search/vector-search-techniques/knn-score-script/)
  documents the exact `knn_score` script and its brute-force cost.
- [The deployed JVector reader](https://github.com/opensearch-project/opensearch-jvector/blob/3.6.0.1-1/src/main/java/org/opensearch/knn/index/codec/jvector/JVectorReader.java)
  constructs an approximate PQ scorer plus an exact on-disk reranker before
  graph search returns candidates.

A production audit ran 50 byte-identical dense requests for each of four
queries. With one stable index/segment snapshot, membership, rank, and score
were identical in all 200 reference searches. Candidate-factor controls at
1x, 2x, and 4x were also internally repeatable, but changing the factor changed
top-50 membership by as much as a Jaccard of 0.33. A safe refresh created no new
segment and preserved three queries exactly. The fourth query received a
different embedding vector from the provider and changed one candidate.
Twenty repeated embedding calls for that text produced two distinct vectors
(maximum coordinate delta about 0.000155; L2 delta about 0.001136).

The historical RRF drift therefore remains evidence of an upstream dense-lane
change, but its old artifacts did not capture the query-vector, request, index,
or lane fingerprints needed to attribute the change exclusively to ANN.

## Decision

The dense contract is:

```text
DENSE_KNN_CONTRACT:
APPROXIMATE_MEMBERSHIP
```

OpenRAG guarantees:

- identical ordered input lanes produce identical RRF output;
- equal RRF scores use canonical persistent chunk identity as the tie-break;
- every RRF product response exposes non-reversible SHA-256 diagnostics for
  the query vector set, successful OpenSearch request, lane membership, lane
  order plus scores, fusion inputs, and fusion output;
- compatibility retries are explicit, so the rejected `num_candidates`
  request is distinguishable from the body OpenSearch actually executed.

OpenRAG does not claim:

- identical ANN membership across index refreshes, merges, plugin upgrades, or
  different query vectors;
- byte-identical embeddings from an external provider;
- that exact reranking can recover a relevant candidate omitted by ANN.

The diagnostic contract is `openrag.retrieval-lane-diagnostics` v1. It exposes
only counts, booleans, and hashes, never hidden chunk or source identities. This
keeps the instrumentation DLS-neutral.

## Rejected alternatives

- Reducing the candidate factor was rejected because it changed membership and
  provides no deterministic guarantee.
- Increasing the factor was not promoted because the current 5x default was
  already repeatable, higher candidate work has a cost, and no recall defect
  attributable to factor 5 was demonstrated.
- Adding a second exact rerank was rejected because JVector already reranks
  exactly, bounded exact-script tests did not recover candidates outside their
  ANN pool, and the extra pass adds latency.
- Rounding embeddings was rejected because it changes similarity semantics and
  cannot create a provider-wide determinism guarantee.
- An embedding cache was rejected for this closeout because it introduces
  lifecycle, privacy, invalidation, and cross-restart semantics beyond a
  default-neutral correctness correction.

## Consequences

Product defaults remain q1, lexical/dense 50/50, RRF k=60, seed budget 100,
and multi-query disabled. The change is observational only. Future drift can
now be classified as vector/request variance, ANN lane variance, or fusion
variance without exposing result identities.

```text
ANN MEMBERSHIP REMAINS APPROXIMATE - FUSION DETERMINISM VALIDATED
```
