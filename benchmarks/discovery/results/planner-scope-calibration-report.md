# Planner Stabilization + Evidence-Based Scope Calibration

## A. Baseline

```text
application_repo:
FermedePommerieux/openrag-compose

branch:
target pommerieux/v0.6.0-retrieval-v2-prov-o
technical work branch agent/planner-calibration

pre_patch_sha:
4d45bba83622d780d999085a449cf7ae697a67d5

previous_worktree:
/Users/eloiprimaux/Developer/openrag-compose-retrieval-correctness

new_worktree:
/Users/eloiprimaux/Developer/openrag-compose-planner-calibration

gitops_repo:
FermedePommerieux/Pommerieux-GitOps

pre_patch_gitops_sha:
5a7d6fb69ab00f30d1da70fba50e9182dcc0926e

cluster:
10.73.50.12

ingress:
https://openrag.ferme-de-pommerieux.fr
```

The technical branch was required because the target branch was already checked out by another retained worktree. No previous worktree was modified. The DLS corpus stayed at 47,454 visible occurrences, 47,400 distinct documents, and digest `038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7` before and after the campaigns.

## B. Planner baseline

```text
model:
configured openai/gpt-5.4-mini
observed gpt-5.4-mini-2026-03-17

request fingerprint count:
1 / 20

plan fingerprint count:
19 / 20

plan Jaccard:
exact query set min 0.1429 / mean 0.1891 / max 1.0000
lexical token min 0.2000 / mean 0.5251 / max 1.0000

seed Jaccard:
min 0.3072 / mean 0.5071 / max 1.0000

scope Jaccard:
min 0.3112 / mean 0.5428 / max 1.0000
```

The 20 calls used the real `/api/search` product path with one runtime fingerprint, temperature 0, non-streaming planner generation, and `max_output_tokens=800`. Two of 20 q4 executions correctly failed closed at the 250-document guard; they are excluded from quality averages and included in the 90% coverage success rate.

## C. Planner variance root cause

```text
provider stochasticity:
YES — dominant; same request/runtime/corpus produced 19 plan fingerprints in 20 calls

query redundancy:
no exact normalized duplicates; within-plan token Jaccard 0.1250–0.4167, mean 0.2383

semantic drift:
literal anchor preserved in the observed case, but varying broad vocabulary was introduced;
diagnostics deliberately produce no automatic semantic verdict

q0 displacement:
40–56 q1 seeds displaced, mean 46;
11–20 relevant q1 occurrences evicted, mean 15.4;
10–18 unique relevant variant occurrences added, mean 13.55;
mean net seed component gain -1.25, mean net scope component gain -1.75

fusion amplification:
YES — every query family has the same RRF treatment and competes inside one global 100-seed budget;
per-query lexical/dense memberships and contributions are now auditable
```

The drift measures are syntax and token diagnostics, not a relevance classifier. Breadth, narrowness, dropped entities, and unrelated concepts still require review when no reliable generic automatic judgment exists.

## D. Strategies tested

| name | algorithm | deterministic | additional LLM calls | cache | generic |
|---|---|---:|---:|---|---:|
| P0 q1 50/50 | canonical query, BM25+dense, RRF | yes | 0 | none | yes |
| P1 q1 deep | canonical query, 100/100 and 200/200 candidate horizons | yes | 0 | none | yes |
| P2 current q4 | q0 plus up to three LLM variants, equal fusion treatment | no | 1 | none | yes |
| P3 bounded plan reuse | reuse the first observed normalized q4 plan for ten fixed-retrieval replays | yes after first plan | 0 during replay | experimental plan reuse only | yes |

P3 deliberately used the first observed plan rather than selecting the best one. It proves that reuse removes retrieval variance for a fixed plan, but it can also freeze a poor plan. No product cache was implemented. A safe future key must cover normalized query, tenant/workspace partition hash, planner provider/model, prompt or request fingerprint, capability profile, max queries, language, and retrieval intent/profile; it must contain no secret and must never cache documentary results. Freshness, invalidation, and a runtime-safe privacy partition remain unresolved.

## E. Planner comparison

Post-PROV-O component recall is the recall column. Surface pastorale is the only human-ground-truth case, so this table is comparative evidence, not a cross-domain product validation.

| strategy | recall | precision | seed stability | scope stability | coverage | latency |
|---|---:|---:|---:|---:|---:|---:|
| P0 q1 50/50, 10 runs | 44.74% | 76.12% | Jaccard 1.000 | Jaccard 1.000 | 10/10 | mean 4.47 s |
| P1 q1 200/200, 1 revalidation | 50.00% | 75.34% | deterministic design; not repeated here | not repeated here | 1/1 | 3.64 s |
| P2 current q4, 20 runs | 43.37% mean on 18 valid runs | 65.91% mean | Jaccard mean 0.507 | Jaccard mean 0.543 | 18/20 | mean 7.08 s on valid runs |
| P3 first-plan reuse, 10 runs | 50.88% | 76.81% | Jaccard 1.000 | Jaccard 1.000 | 10/10 | mean 4.49 s |

P3's fixed plan recovered 54.17% of relevant documents post-PROV-O. Its quality equals the original run that produced that plan; reuse removed variance rather than creating a recall gain.

## F. q1 deep discovery

```text
50/50:
Seed Component 35.96%; Post Component 44.74%; Post Document 45.83%; Precision 76.12%; natural scope 138; coverage complete; 5.49 s revalidation

100/100:
Seed Component 36.84%; Post Component 46.49%; Post Document 46.88%; Precision 75.00%; natural scope 140; coverage complete; 3.81 s revalidation

200/200:
Seed Component 40.35%; Post Component 50.00%; Post Document 50.52%; Precision 75.34%; natural scope 147; coverage complete; 3.64 s revalidation

cross-domain evidence:
No second human ground truth was available. The existing generic contract case is synthetic and was not promoted to human truth.
```

The latency ordering is observational and not evidence that larger horizons are intrinsically faster. The 200/200 quality signal is promising but remains mono-ground-truth.

## G. Scope calibration dataset

```text
queries:
7 generic intents × q1/q4-fixed = 14 closures

closures measured:
Exact invoice 2/288
Fibre correspondence 205/242
Contract Alpha 343/254
Network outage 389/345
Dupont history 162/214
Surface investigation 138/212
Solar correspondence 164/199

p50:
212

p90:
345

p95:
389

p99:
389

max:
389 documents, 389 entities, 17,294 chunks, depth 5
```

Natural scopes ranged from 2 to 389 documents and 7 to 17,294 chunks. `max_depth=8`, `max_entities=500`, and `batch_size=50` were held fixed.

## DOCUMENTARY TARGET VALIDATION

```text
target_threshold_tested:
200, 250, 300

validation_probe_sizes_tested:
25, 50

hard_safety_limits_tested:
400, 500

probe structural signals:
frontier before/after and growth rate; new documents/entities/documentary relations/connected branches;
depth before/after; relation-type delta; already-covered ratio; hub/degree diagnostics

number_of_queries:
14 for target 250 + probe 50 + hard 500; 7 long/borderline closures for the parameter variants

closures_completed_before_target:
9 / 14 at target 250

closures_requiring_probe:
5 / 14

closures_requiring_multiple_extensions:
3 / 14

hard_limit_hits:
0

false_target_rate_at_250:
5 / 14 = 35.71%

recommended_target_threshold:
NO PRODUCT DEFAULT — 250 is the best-supported experimental checkpoint, not a stopping rule

recommended_probe_size:
NO PRODUCT DEFAULT — 50 is the more efficient experimental batch among 25/50 replays

recommended_hard_safety_limit:
NO PRODUCT DEFAULT — unresolved; 400 leaves only 11 documents above the observed maximum and 500 had no hard-limit hit

documentary_semantics:
probe-beyond-target-to-validate-target
```

For target 250 / probe 50 / hard 500, all 14 sampled closures reached natural exhaustion, with 0–3 probes (mean 0.64). Fixed 250 completed 9/14 and truncated 5/14 legitimate closures; fixed 400 and fixed 500 completed 14/14. The prototype intentionally has no relevance or marginal-yield early stop:

```text
TARGET_THRESHOLD:
provisional cost/diagnostic checkpoint; 250 tested, not a certified limit

VALIDATION_PROBE:
bounded deterministic traversal beyond the checkpoint; 25 and 50 tested

HARD_SAFETY_LIMIT:
mandatory absolute fail-closed guard; 400 and 500 tested, value not calibrated

ADAPTIVE_CONTINUE_CRITERIA:
frontier non-empty AND hard safety limit not reached

ADAPTIVE_STOP_CRITERIA:
none in exhaustive mode; only NATURAL_COMPLETE or HARD_SAFETY_LIMIT_REACHED
```

The same marginal yield of 50 new documents corresponded to frontier growth rates from -0.947 to +1.833 and to different depth transitions. Network-outage q1 evolved from frontier 269 to 42, then 119, then 0 while the first two probes each yielded 50 documents. Marginal yield alone is therefore insufficient; frontier and depth dynamics carry essential structural information. These signals remain diagnostics and never convert a non-empty frontier into completeness.

For exhaustive mode, staged validation ultimately reads the same natural closure as a sufficiently high hard limit. Its current advantage is auditable checkpoint semantics, not proven read-volume reduction. The calibration runner restarts deterministic traversal at each target, so its cumulative probe latency overstates a future stateful implementation.

Architecture recommendation: `3. INSUFFICIENT EVIDENCE`.

```text
DOCUMENTARY_TARGET_VALIDATION:
PROMISING_BUT_INSUFFICIENT_EVIDENCE
```

## H. Resource impact

```text
documents:
2–389 natural; fixed-250 long-case mean 250 incomplete; fixed-400/500 long-case mean 323.8 complete

chunks:
7–17,294; long-case mean 9,937 at fixed 250 versus 10,557 at natural completion

graph latency:
24 runs min 0.022 / mean 0.413 / max 0.816 s

document-read latency:
24 runs min 1.855 / mean 4.761 / max 9.332 s

total latency:
24 runs min 1.877 / mean 5.877 / max 10.653 s;
five >250 closures mean 6.23 s incomplete at 250, 7.16 s complete at 400, 7.30 s complete at 500

transport payload:
unchanged runtime-safe projection; 96 model chunks in the 10,000-chunk regression and serialized size <1.01× the 100-chunk projection for the same document manifest
```

CPU was not observable through the runner. `ru_maxrss` deltas ranged from 0 to 60,352 KiB, but this is a process high-water mark and is order-dependent; it is not a reliable comparative RAM benchmark. No thousands-of-chunks scope payload was sent to Langflow.

## I. max_documents

```text
current:
250

recommended:
NO FIXED REPLACEMENT and NO DEFAULT CHANGE

evidence:
250 truncates 5/14 legitimate closures; 400 and 500 recover this sample, but 400 has an 11-document margin and no pathological closure calibrated the mandatory hard guard

confidence:
high that 250 is not a safe completeness guard; low that 400 or 500 is a generally safe replacement
```

## J. max_entities

```text
current:
500

recommended:
NO DEFAULT CHANGE

evidence:
natural maximum observed 389 entities; no entity guard hit; a higher document hard guard can make max_entities the next limiting contract

confidence:
insufficient to recalibrate
```

`max_depth=8` remains unchanged because observed natural depth was at most 5. `batch_size=50` remains unchanged and is not treated as a documentary limit.

## K. Planner recommendation

`KEEP Q1 AS DEFAULT`

Current q4 has lower mean recall and precision, weak seed/scope stability, and only 90% coverage on the single human-ground-truth case. P3 proves stabilization but not safe generic quality, privacy partitioning, or cross-domain benefit. Multi-query remains opt-in/experimental; no automatic routing was introduced.

## L. Candidate horizon recommendation

`INSUFFICIENT CROSS-DOMAIN EVIDENCE`

The 200/200 result is promising, but the required second independent human ground truth is absent. Default 50/50 is unchanged.

## M. Scope limit recommendation

`CALIBRATION STILL INSUFFICIENT`

The current 250 should be interpreted as an experimental target checkpoint in the target-validation design, but the deployed product still uses it as its existing fail-closed guard because no product default was changed. A product migration requires a calibrated hard safety limit and resource evidence from explosive closures.

## N. Files changed

Application:

- `benchmarks/discovery/remote_retrieval_correctness.py`: fixed-plan replay, target-validation prototype, structural probe diagnostics, graph/read timing.
- `benchmarks/discovery/retrieval_correctness_analysis.py`: multi-capture planner analysis, q0 competition/contribution accounting, semantic diagnostics, target/fixed/resource aggregation.
- `tests/unit/benchmarks/test_retrieval_correctness_analysis.py`: multi-capture, drift, contribution, frontier/depth, and hard-limit fail-closed tests.
- `benchmarks/discovery/results/planner-scope-calibration-analysis.json`: consolidated machine-readable analysis.
- Planner, horizon, stabilization, target-validation, parameter, and resource captures listed in the evidence commit.
- `benchmarks/discovery/results/planner-scope-calibration-report.md`: this report.

GitOps:

- none.

Product behavior/defaults:

- none.

## O. Tests

```text
planner:
PASS — fingerprint, normalization/order, q0 preservation and contribution suites included

cache:
NOT APPLICABLE — no product cache implemented; first-plan reuse was benchmark-only

retrieval:
PASS

scope:
PASS — boundaries, natural diagnostics, target probe diagnostics

coverage:
PASS — hard/document/entity guards remain fail-closed

runtime-safe:
PASS — payload remains bounded by model results and compact document manifest

benchmark:
PASS — real product captures plus deterministic in-pod calibration runner

targeted:
140 passed, then 4/4 updated analysis tests passed after the final hard-limit test

full suite:
tests/unit PASS (1,572 tests after final test); repository-wide bare pytest has a pre-existing SDK/module-name collection conflict at sdks/python/tests/test_retrieval_provenance.py

Ruff:
PASS on changed files

Mypy:
PASS on changed files

git diff check:
PASS
```

## P. Production

```text
source_sha:
74263a9b812a2eb0a83bc676cd37d3dfe82c0e1a

backend_tag:
v0.6.0-retrieval-v2.80

backend_digest:
sha256:7d0a46c39b004a680628a9d8e29cba8ca46840a3b53e4f762af942163aaebb07

langflow_tag:
v0.6.0-retrieval-v2.71

langflow_digest:
sha256:ca996229aa0da00a6ea2f6526be950482305b468e115259615ac72e76e8f1949

frontend_tag:
v0.6.0-retrieval-v2.67

frontend_digest:
sha256:937d153573d96e905918c51ab839867971af4c7b63a946300131995f601a9d61

gitops_sha:
5a7d6fb69ab00f30d1da70fba50e9182dcc0926e

Fleet:
8/8 bundle deployments Ready
```

No functional change was justified, so no image was built, no GitOps commit was created, and no rollout was performed. RuntimeBehaviorProfile stayed `MATCH`, fingerprint `8f5bc0f62c5f2b7ffcb08ff18301257dd97ae0f2add22da2ba647609fa30400b`, with configured/effective agent and planner both `openai/gpt-5.4-mini`.

## Q. Cluster

```text
10.73.50.12

nodes:
5/5 Ready

backend:
1/1 Ready, 0 restarts

Langflow:
3/3 Ready, 0 restarts

frontend:
1/1 Ready, 0 restarts

OpenSearch:
green, 17/17 active shards, 0 unassigned

restarts:
OpenRAG core 0; existing openarchiver connector 22

OOM:
OpenRAG core 0; connector last terminated OOMKilled before this read-only chantier

CrashLoop:
0
```

## R. Ingress

```text
https://openrag.ferme-de-pommerieux.fr

frontend:
HTTP 200

health:
HTTP 200

search:
HTTP 200 via /api/search

streaming:
HTTP 200; Content-Type text/event-stream; response.completed observed via /api/langflow
```

## S. Remaining audit

```text
AUD-008
AUD-009
live cross-user DLS
```

Not started.

## T. Qwen

```text
QWEN_READINESS:
BLOCKED
```

No Qwen, EmbeddingGemma, re-embedding, or vector-index work was performed.

## U. Conclusion

`PLANNER AND SCOPE CALIBRATION PARTIAL`

Planner variance is measured and causally localized; q1 remains the justified default. Deep q1 and first-plan reuse are promising but lack independent human ground truth. Target validation is structurally sound and preserves fail-closed completeness, but the hard safety limit and pathological-resource behavior remain uncalibrated. No product or GitOps default changed.
