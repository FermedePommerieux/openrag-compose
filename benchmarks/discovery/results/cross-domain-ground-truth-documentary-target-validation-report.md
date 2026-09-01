# Cross-Domain Ground Truth + Documentary Target Validation

## A. Git baseline

```text
application_repo:
FermedePommerieux/openrag-compose

target_branch:
pommerieux/v0.6.0-retrieval-v2-prov-o

starting_branch:
agent/planner-calibration

starting_sha:
f126c5439663585da5ff9c6419febfbb7a308a75

target_remote_sha:
4d45bba83622d780d999085a449cf7ae697a67d5

previous_worktree:
/Users/eloiprimaux/Developer/openrag-compose-planner-calibration

new_worktree:
not created — explicit user override continued in the previous/current worktree

gitops_repo:
FermedePommerieux/Pommerieux-GitOps

gitops_sha:
5a7d6fb69ab00f30d1da70fba50e9182dcc0926e
```

The two existing commits `7df2373a` and `f126c543` remained reachable and unchanged. No branch switch, reset, cleanup, or new worktree occurred.

## B. Cluster / ingress

```text
cluster:
10.73.50.12

ingress:
https://openrag.ferme-de-pommerieux.fr
```

Post-campaign verification: 5/5 nodes Ready; backend 1/1, Langflow 3/3, frontend 1/1; Fleet 8/8; public `/api/health` OK. RuntimeBehaviorProfile is `MATCH`, fingerprint `8f5bc0f62c5f2b7ffcb08ff18301257dd97ae0f2add22da2ba647609fa30400b`. Production remains q1, 50/50, seed 100, RRF 60, documentary-prov-o v1, depth 8, entities 500, documents 250, batch 50.

The DLS-visible corpus was re-enumerated after the campaign: 47,454 occurrences, 47,400 distinct document IDs, digest `038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7`, complete. It is unchanged.

## C. Ground truth #1

```text
case:
Surface pastorale

counts:
CORE 114 components / 192 documents
CONTEXTUAL 28 components / 59 documents
NOT_RELEVANT 5 components / 11 documents

digest:
43963a033a203aa29f62d518e6b940749b4cba62ed1cb9c44586ddcac8c97341
```

The human labels were not changed.

## D. Ground truth #2

```text
case:
orange-fibre-cross-domain-v1 candidate review universe

domain:
technical connectivity correspondence and fibre installation dossier

review process:
canonical q1 + lexical/dense/RRF deep controls + entity/alternate/outside controls + bounded metadata/thread recovery; all relevance labels left empty

candidate count:
3,012 documents / 1,338 components captured; first human-review tranche 850 documents / 350 components

CORE components:
not available — human review pending

CORE documents:
not available — human review pending

CONTEXTUAL:
not available — human review pending

NOT_RELEVANT:
not available — human review pending

ground_truth_digest:
not available — GT2 is not frozen

human_review_complete:
false
```

The compact review artifact digest is `1bfd78cb003845afb2f8a8076ab024507b54394d41de92109ee08517bde20d33`; this is an artifact digest, not a ground-truth digest. All 850 document and 350 component `human_label` fields are empty. The compact full candidate capture contains all 3,012 candidates and no filled label.

## E. GT2 completeness controls

```text
initial candidates:
3,012 documents / 1,338 components in the deduplicated compact pool

outside-closure controls:
677 of 850 selected documents are outside the current 205-document q1 closure; 70 selected documents are control-priority candidates

additional candidates found:
422 selected documents are metadata/relation recovery members with no direct query-lane hit

final human review:
pending

remaining ambiguity:
all labels remain human-owned; 988 components / 2,162 documents are explicitly deferred to later review/completeness tranches, not classified as irrelevant
```

After the initial human tranche, completeness control must revisit deferred candidates and run exact-title, alias, correspondent, disconnected-component, thread-linked copy, lexical-deep, dense-deep, and alternative-wording checks. GT2 must not be frozen before those additions are also reviewed.

## F. Candidate horizon — GT1

| Horizon | Seed document recall | Seed component recall | Post document recall | Post component recall | Strict precision | Expansion / seed document | Coverage | Wall latency |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| 50/50 | 26.56% | 35.96% | 45.83% | 44.74% | 76.12% | 2.060 | complete | 5.49 s |
| 100/100 | 26.56% | 36.84% | 46.88% | 46.49% | 75.00% | 2.059 | complete | 3.81 s |
| 200/200 | 28.65% | 40.35% | 50.52% | 50.00% | 75.34% | 2.014 | complete | 3.64 s |

The latency ordering is observational, not a claim that larger horizons are intrinsically faster.

## G. Candidate horizon — GT2

| Horizon | Seed document recall | Seed component recall | Post document recall | Post component recall | Strict precision | Expansion | Coverage | Latency |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| 50/50 | not measured | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| 100/100 | not measured | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| 200/200 | not measured | not measured | not measured | not measured | not measured | not measured | not measured | not measured |

Running these metrics before human review would let retrieval output grade itself and is therefore prohibited.

## H. Cross-domain horizon conclusion

```text
GT1 winner:
200/200 on measured recall with small observed precision change

GT2 winner:
not available

consistent:
no — cross-domain comparison is not yet possible

recommendation:
INSUFFICIENT CROSS-DOMAIN EVIDENCE
```

Production stays 50/50.

## I. Closure calibration dataset

```text
number_of_intents:
32

number_of_closures:
32 q1 deterministic natural closures (plus the previous 14 q1/q4-fixed closures as corroborating evidence)

p50:
203

p75:
249

p90:
294

p95:
367

p99:
389

max:
389

max entities:
389

max chunks:
20,456

max depth:
6
```

All 32 frontiers exhausted naturally below the experimental document guard of 750. Natural closure is therefore known for every sampled q1 case.

## J. Sampling methodology

The 32-query list and order were versioned before measurement. It is stratified across precise entity lookup, correspondence investigation, technical incident, contractual history, person/entity chronology, project history, multi-party topic, small documentary component, and pathological-candidate forms. The list includes exact two-document retrieval, small/medium scopes, known large closures, and three deliberately broad generic stressors. No query was added or removed after observing its size. Percentile convergence is recorded after every ordered case; at n=20 it was 203/228/262/343/389, at n=28 it was 203/241/271/343/389, and at n=32 it was 203/249/294/367/389 for p50/p75/p90/p95/p99.

The diagnostic-only entity guard was 1,000 so a document hard guard up to 750 could be isolated with 33% headroom under the approximately one-entity-per-occurrence graph. Production `max_entities=500`, `max_depth=8`, and traversal batch 50 were not changed; depth and batch were not overridden.

## K. Large/outlier closures

| Case | Documents | Entities | Chunks | Depth | Frontier trajectory at target/probe observations | Largest degree | Relation types | Outcome |
|---|---:|---:|---:|---:|---|---:|---|---|
| Intermittent network outage | 389 | 389 | 17,076 | 5 | 197→226→269→298→42→80→119→42→0 | 17 | attachment_of, member_of, references, reply_to | NATURAL_COMPLETE |
| All farm exchanges/documents | 367 | 367 | 5,504 | 5 | 195→226→251→9→74→106→36→0 | 13 | attachment_of, member_of, references, reply_to | NATURAL_COMPLETE |
| Contract Alpha renewal | 343 | 343 | 7,835 | 5 | 205→239→258→45→72→15→0 | 24 | attachment_of, member_of, references, reply_to | NATURAL_COMPLETE |
| All emails and attachments | 294 | 294 | 7,122 | 5 | 160→183→4→5→0 | 21 | attachment_of, member_of, references, reply_to | NATURAL_COMPLETE |
| Meeting/attachments/construction | 271 | 271 | 6,596 | 3 | 169→193→13→0 | 13 | attachment_of, member_of, references, reply_to | NATURAL_COMPLETE |

The broad stressors did not produce a hard-guard hit. The largest observed closure remains a legitimate documentary graph, not an infrastructure expansion.

## L. Infrastructure hub regression

```text
email_archive reverse:
0 traversed expansions — non-expansive

directory_collection reverse:
0 traversed expansions — non-expansive

ingestion-root reverse:
0 traversed expansions — non-expansive
```

The aggregate target/probe grid contains zero forbidden infrastructure reverse-expansion rows.

## M. Target threshold experiments

| Target | Closures before target | Closures needing probe | Multiple probes | Legitimate truncation if fixed |
|---:|---:|---:|---:|---:|
| 200 | 15/32 | 17/32 | 8/32 with probe 50; 11/32 with probe 25 | 17/32 |
| 250 | 24/32 | 8/32 | 3/32 with probe 50; 4/32 with probe 25 | 8/32 |
| 300 | 29/32 | 3/32 | 2/32 with probe 50; 3/32 with probe 25 | 3/32 |

These are cost-onset observations, not truth limits. A target at 300 reduces probe frequency, but the sample does not establish that its delayed validation is preferable on Raspberry Pi resources.

## N. Probe-size experiments

The fully crossed hard-750 grid gives:

| Probe size | Mean probes | Max probes | Mean prototype replay overhead | Max replay overhead | Frontier diagnostic quality |
|---:|---:|---:|---:|---:|---|
| 25 | 0.823 | 8 | 0.300 s | 3.17 s | higher-resolution frontier/depth trajectory |
| 50 | 0.500 | 4 | 0.168 s | 1.46 s | sufficient to expose active frontier with fewer replay queries |

At target 250 specifically, probe 25 averaged 0.656 extensions (max 6) versus 0.406 (max 3) for probe 50. The benchmark prototype replays traversal from the seed at every extension; a product implementation should retain state, so replay overhead is an upper-bound artifact rather than a projected continuous traversal cost.

Marginal document yield alone is not sufficient. Across probes adding exactly 25 documents, observed frontier growth ranged from approximately -0.99 to +39.0 with depth transitions from 1→1 through 3→5. For 50-document probes it ranged approximately -0.99 to +1.83. Frontier and depth dynamics therefore add essential, deterministic diagnostic information.

## O. Hard-limit experiments

| Hard limit | Legitimate closure truncations | Pathological cases stopped | Max observed resources below guard | Coverage behavior |
|---:|---:|---:|---|---|
| 400 | 0/32 | 0 | 389 docs / 389 entities / 20,456 chunks | 32/32 natural complete; mutation contract makes any guard hit incomplete |
| 500 | 0/32 | 0 | same | 32/32 natural complete; no live hard hit |
| 750 | 0/32 | 0 | same | 32/32 natural complete; no live hard hit |

Hard 400 has only 11 documents of observed headroom over the maximum and is not “comfortably above” legitimate closures. Hard 500 and 750 were not exercised as guards by a real pathological closure. Therefore none is calibrated as a new product hard guard.

## P. Documentary target validation

```text
TARGET_THRESHOLD:
200, 250, and 300 tested. Target 250 remains the best-understood experimental cost onset, not a completeness limit and not a recommended default.

VALIDATION_PROBE:
25 and 50 tested. Probe 50 produced fewer replay extensions while preserving the exhaustive continuation signal; continuous-state resource evidence is still needed before calibration.

HARD_SAFETY_LIMIT:
400, 500, and 750 tested as absolute diagnostic guards. No guard was hit, so calibration is insufficient.

semantics:
probe-beyond-target-to-validate-target
```

The exhaustive rule is deterministic: natural frontier exhausted successfully → `NATURAL_COMPLETE` and coverage complete; frontier non-empty below hard → continue; hard or another safety guard reached → `HARD_SAFETY_LIMIT_REACHED` and coverage incomplete. There is no LLM decision, semantic relevance stop, adaptive best-effort completion, or `ADAPTIVE_STOP` presented as closure.

## Q. Fixed vs adaptive architecture

| Strategy | Completion rate | Legitimate truncation | Mean graph latency | Mean documents | Probe/replay overhead | Hard-hit rate |
|---|---:|---:|---:|---:|---:|---:|
| fixed 250 | 75.0% | 25.0% (8/32) | 0.304 s | 190.1 | none | 25.0% document-limit hits |
| fixed 400 | 100% | 0% | 0.336 s | 203.9 | none | 0% |
| fixed 500 | 100% | 0% | 0.354 s | 203.9 | none | 0% |
| target 250 / probe 50 / hard 500 prototype | 100% | 0% | 0.343 s final traversal | 203.9; mean 13.84 beyond target | mean 0.406 extra graph queries and 0.147 s replay overhead | 0% |

Fixed 400 and 500 happen to recover this sample, but the absence of a pathological hard hit means their safety role is untested. Target/probe/hard correctly separates validation onset from safety semantics and is supported by 32/32 fail-closed natural outcomes, yet production adoption still needs a real guard-hit/resource case and a continuous-state implementation measurement.

Document reads were measured through the product service on representative required closures. Contract Alpha: fixed 250 read 250 docs/7,011 chunks in 4.63 s and remained incomplete; fixed 400 read the natural 343 docs/7,835 chunks in 5.43 s and completed. Network outage: fixed 250 read 250 docs/15,911 chunks in 7.22 s and remained incomplete; fixed 400 read the natural 389 docs/17,076 chunks in 9.33 s and completed. The target/probe graph prototype intentionally did not re-read all document text at every replay; a retained-state implementation should perform the same final natural document read plus incremental traversal.

## R. Resource impact

```text
graph latency:
0.021–0.816 s across fixed/resource graph observations; target-250/probe-50/hard-500 final mean 0.343 s

document-read latency:
1.86–9.33 s on q1 product-service resource cases

total latency:
1.88–9.98 s on q1 resource cases

largest chunk count:
20,456 in the closure grid; 17,076 in a full product-service document-read run

memory:
ru_maxrss deltas 0–60,352 KiB in resource runs, but process high-water deltas are order-dependent and not a reliable comparative RAM metric

CPU:
not reliably observable for per-experiment attribution; no value fabricated

Langflow payload:
PASS — synthetic 25,000-chunk transport test still projects only 96 model results and omits evidence_batches/full graph/all chunk text
```

The Raspberry Pi cluster remained Ready after both captures.

## S. Product recommendation

```text
DEFAULT RETRIEVAL:
q1

CANDIDATE HORIZON:
INSUFFICIENT CROSS-DOMAIN EVIDENCE

DOCUMENTARY SCOPE ARCHITECTURE:
TARGET/PROBE/HARD PROMISING BUT INSUFFICIENT EVIDENCE

HARD GUARD:
HARD GUARD CALIBRATION INSUFFICIENT
```

Current product settings remain the temporary guardrails; this is not a claim that fixed 250 is adequate documentary coverage.

## T. Product changes

```text
product default changed:
no

build:
no

GitOps:
no

deployment:
no
```

There was no index change, re-ingestion, model/prompt change, planner cache, q4 routing, or Qwen work.

## U. Tests

```text
ground_truth:
PASS — existing loader/provenance validation; synthetic contract fixture explicitly excluded from GT2

review tooling:
PASS — empty-label invariant, metadata target filtering, component grouping, outside-closure priority, CSV/JSON parity, compact capture deduplication

candidate horizon:
PASS — frozen single-axis 50/50, 100/100, 200/200 GT1 captures retained; GT2 correctly gated on human labels

scope sampling:
PASS — 32 unique preordered intents, >=8 families, required known closures, no q4 plans, parameter-grid assertions

target/probe:
PASS — target continuation and probe structural diagnostics

hard guard:
PASS — live grid plus state-machine mutation cases

coverage:
PASS — natural complete iff successful frontier exhaustion; all guard states fail closed

runtime-safe:
PASS — 25,000-chunk Langflow projection test

benchmark:
PASS — tool/config/evidence hashes, runtime fingerprint, corpus digest, DLS descriptor, settings, identities, outcomes, and timings captured

targeted:
26 passed before full-suite run; cross-domain review suite 5 passed after compact-capture addition

unit suite:
1,587 passed, 94 warnings

Ruff:
PASS on all changed source/test files

Mypy:
PASS on four changed benchmark source modules

git diff check:
PASS — no whitespace errors; existing commits preserved; ignored evidence explicitly selected for versioning
```

## V. Remaining audit

```text
AUD-008
out of scope; unchanged

AUD-009
out of scope; unchanged

live cross-user DLS
out of scope; unchanged
```

## W. Qwen

```text
QWEN_READINESS:
BLOCKED
```

No embedding model, vector index, or re-ingestion work was started.

## X. Conclusion

```text
CROSS-DOMAIN EVIDENCE STILL INSUFFICIENT
```

GT2 human review and completeness control are the blocking evidence for candidate-horizon generalization. Documentary target validation is materially stronger, but hard-guard calibration remains open because no legitimate graph exercised a hard stop. No product change is justified.
