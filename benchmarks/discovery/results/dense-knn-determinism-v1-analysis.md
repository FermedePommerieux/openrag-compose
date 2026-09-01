# Dense KNN determinism v1 — production analysis

## Captures

- Main capture: `dense-knn-determinism-v1.json`
  (`faca3d12195d39bf80d72bc0b10ce4ec0ce5a406f19e1bc7953963ecac2962c4`)
- Post-refresh capture: `dense-knn-post-refresh-v1.json`
  (`8990f81905e89f464aed3c8aa3c1c1ff5fcb0e4cf9c890e7abbde852db1dd09d`)
- OpenSearch: 3.6.0, Lucene 10.4.0, index UUID
  `9B179oe3TzWLZKpg77_XRQ`, one primary shard, no replica.
- Field: `chunk_embedding_text_embedding_3_large`, dimension 3072,
  JVector `disk_ann`, L2, `m=16`, `ef_construction=100`.
- Stable observed state: 19 segments; the safe refresh succeeded and left the
  segment fingerprint unchanged.

## Byte-identical reference campaign

Each query was embedded once and its exact vector reused for 50 searches.

| query | runs | ordered sets | membership sets | min Jaccard | max rank displacement | score changes | mean wall ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| GT1 q1 50/50 | 50 | 1 | 1 | 1.0 | 0 | 0 | 44.27 |
| GT2 q1 50/50 | 50 | 1 | 1 | 1.0 | 0 | 0 | 42.99 |
| generic technical | 50 | 1 | 1 | 1.0 | 0 | 0 | 41.26 |
| generic contractual | 50 | 1 | 1 | 1.0 | 0 | 0 | 42.79 |

Every run records the request fingerprint, vector hash, index UUID, segment
snapshot, ordered chunk/source identities, score, rank, OpenSearch time and
wall time.

## Candidate-factor controls

JVector defaults to an exact rerank candidate factor of 5. Factors 1, 2 and 4
were each repeated ten times per query. Every factor was internally stable,
but its membership could differ from factor 5:

| factor | worst membership Jaccard vs 5x | largest rank displacement | mean-latency ratio range vs 5x |
|---:|---:|---:|---:|
| 1x | 0.3333 | 24 | 0.70–0.77 |
| 2x | 0.7544 | 7 | 0.72–0.85 |
| 4x | 0.7544 | 7 | 0.86–1.01 |

GT1/GT2 dense occurrence recall did not materially improve at the lower
factors. Factor reduction is therefore not a correctness fix.

## Refresh and embedding isolation

The refresh completed on the single primary shard and kept 19 segments with
the same segment fingerprint. GT1, GT2 and the technical query retained the
same vector, request, membership, order and scores. The contractual query used
a different provider-returned vector and changed one top-50 member
(Jaccard 0.9608).

Twenty immediate calls for that exact text returned two distinct
`text-embedding-3-large` vectors. The exceptional vector differed by at most
`0.00015497207641601562` per coordinate and by about
`0.001135837538446593` in L2 norm. Old RRF artifacts did not capture vector
hashes, so historical dense-rank drift cannot be attributed exclusively to ANN.

## Exact rerank control

OpenSearch `knn_score` exact scoring was run on fixed ANN pools of 50, 100 and
200 candidates for all four queries, with three exact repetitions per pool.
All 12 experiments kept one exact order; one experiment had bit-level score
variation without an order change. The exact top-50 changed from pool 50 to
pool 100 for all four queries, and was identical from pool 100 to pool 200.
This demonstrates both properties relevant to the contract:

1. exact scoring can deterministically order a fixed candidate set;
2. it cannot recover an item omitted from the ANN candidate set.

JVector already implements approximate PQ candidate generation followed by an
exact on-disk rerank. A second product rerank would add cost without addressing
embedding or candidate-membership variation.

## Decision

```text
ANN MEMBERSHIP REMAINS APPROXIMATE - FUSION DETERMINISM VALIDATED
```

The product defaults remain q1, 50/50, RRF 60, seed 100 and
`multi_query=false`. The accepted code change adds only non-reversible
diagnostic hashes described by `openrag.retrieval-lane-diagnostics` v1.
