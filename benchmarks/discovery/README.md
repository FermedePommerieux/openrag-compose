# Discovery benchmark — OpenAI / Retrieval v2 baseline

This directory measures the frozen BM25 + dense + RRF discovery baseline and
the documents recovered from those seeds by `documentary-prov-o` version 1.
It does not tune retrieval or mutate OpenSearch, runtime settings, embeddings,
GitOps, ingestion, or deployment state.

## Metric contract

`K` is the production top-K **ranked chunk** budget. Document metrics
de-duplicate chunks by `source_entity_id`, because one content-derived
`document_id` can legitimately have multiple source occurrences. The ground
truth therefore requires both identities and declares
`document_metric_unit: source_occurrence`.

Strict metrics exclude `uncertain` items. Precision also reports review
coverage and is final only when every returned seed occurrence is classified.
`coverage.complete` is reported independently and is never treated as recall.

The primary KPI is Seed Component Recall@K:

```text
human-validated relevant components with >= 1 seed occurrence in top-K chunks
-------------------------------------------------------------------------
all human-validated relevant components
```

Other metrics implemented in `metrics.py` are Seed Document Recall, post-PROV-O
document/component recall, precision, both expansion factors, document recovery
gain, recovery multiplier, and coverage success rate.

Each machine-readable run also carries a compact ranked seed capture (chunk,
document, occurrence, available lane ranks/scores), the exact query, frozen
retrieval/embedding metadata, timestamp, and before/after DLS corpus snapshot.

## Ground truth workflow

1. Run one literal fused capture and one full `scope_exhaustive` capture.
2. Build compact candidate JSON and CSV:

   ```bash
   uv run python -m benchmarks.discovery.harness review \
     --focused /tmp/focused.json \
     --scope /tmp/scope.json \
     --output-json benchmarks/discovery/results/surface-pastorale-v1-review.json \
     --output-csv benchmarks/discovery/results/surface-pastorale-v1-review.csv
   ```

3. A human classifies candidates and defines components in
   `definitions/surface-pastorale-v1.yaml`. Retrieval output must never be copied
   into ground truth as automatically relevant.
4. Evaluate the reviewed definition:

   ```bash
   uv run python -m benchmarks.discovery.harness evaluate \
     --definition benchmarks/discovery/definitions/surface-pastorale-v1.yaml \
     --focused /tmp/focused.json \
     --scope /tmp/scope.json \
     --output-json benchmarks/discovery/results/surface-pastorale-v1-baseline.json \
     --output-csv benchmarks/discovery/results/surface-pastorale-v1-summary.csv \
     --retrieval-variant rrf \
     --k 20 --k 50 --k 100 --k 200
   ```

5. Snapshot the exact DLS-visible corpus before and after a comparative run:

   ```bash
   uv run python -m benchmarks.discovery.harness corpus \
     --base-url https://openrag.example.test \
     --output /tmp/corpus-before.json
   ```

The corpus collector follows the server's `search_after` cursors and hashes the
sorted occurrence identities. A comparison is directly comparable only when
the count and identity digest are unchanged.

## Canonical final replay

`final_baseline.py` runs domain-neutral benchmark instrumentation inside the
existing backend runtime. It sends the versioned literal query directly to the
same OpenSearch lexical and dense bodies, reuses the production RRF and
per-document limiting functions, and calls the production certified PROV-O
closure for every distinct seed set. It does not change runtime settings.

The configured candidate horizons remain part of the evaluated system. With
50 lexical and 50 dense candidates, a requested K above the emitted lane length
uses every available ranked chunk and records both `requested_k` and
`effective_seed_chunks`. Identical effective seed sets reuse the exact same
closure measurement and say so explicitly. No missing rank or closure is
inferred from control-search probes.

The runner emits STRICT and BROAD metrics, lane contributions, complete
coverage certificates, canonical-query miss ranks, performance measurements,
before/after corpus snapshots, and reusable machine-readable captures for a
future dense-model replay.

## Generic multi-query experiment

`multi_query_benchmark.py` evaluates the opt-in query-diversity layer without
making cluster changes. The in-pod runner receives only the literal query,
frozen retrieval settings, bounded query/concurrency values, the final seed
budget, and the unchanged PROV-O scope limits. It cannot read the benchmark
definition or human ground truth. Ground-truth scoring happens locally only
after all ranked results have been captured.

The primary replay fixes `final_seed_budget` to the effective q1 baseline
budget (96 for this frozen benchmark), so any recall difference comes from
query diversity rather than a larger candidate pool. Runs are cumulative:
q1 is the exact original query, then q2 through q4 add one generated query at a
time. A separate budget-100 capture may be retained only as a clearly named
secondary control.

```bash
uv run python -m benchmarks.discovery.multi_query_benchmark capture \
  --definition benchmarks/discovery/definitions/surface-pastorale-v1.yaml \
  --remote-script benchmarks/discovery/remote_multi_query.py \
  --output benchmarks/discovery/results/surface-pastorale-v1-multi-query-capture.json \
  --base-url https://openrag.example.test \
  --ssh-host user@cluster.example.test \
  --ssh-key /absolute/path/to/key \
  --namespace openrag \
  --deployment openrag-backend
```

Evaluation requires no cluster access and writes JSON, CSV, and the structured
decision report. Supplying a validation JSON makes test and lint status part of
the decision gate.
