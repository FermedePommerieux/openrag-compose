# Product-path discovery benchmark

benchmark_id: surface-pastorale-v1
runtime_source_sha: 36c5afdec6a09ea809a9a0e733de191de1b578c7
product_endpoint: https://openrag.ferme-de-pommerieux.fr/api/search
DLS_identity: anonymous
global_seed_budget: 100

## Corpus

visible_occurrences: 47454
distinct_documents: 47400
digest_before: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
digest_after: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
comparable: true

## STRICT

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 76.1% [76.1%, 76.1%] | 2.060 [2.060, 2.060] | 4.435 [3.963, 4.813]s | 3/3 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.7% [26.7%, 26.7%] | 38.0% [38.0%, 38.0%] | 53.8% [53.8%, 53.8%] | 50.7% [50.7%, 50.7%] | 100.0% [100.0%, 100.0%] | 2.060 [2.060, 2.060] | 4.435 [3.963, 4.813]s | 3/3 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

## Invalid runs

None.

## Historical comparison (STRICT)

| q | Metric | Historical | Product mean | Delta |
|---:|---|---:|---:|---:|
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | -0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.836 | 4.435 | -0.401 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | -0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.731 | 4.435 | +0.704 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | -0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.957 | 4.435 | +0.478 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | -0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.172 | 4.435 | +0.263 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | -0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.705 | 4.435 | +0.730 |

## Contract audit

all_contracts_valid: true
all_configurations_fully_valid: true
best_query_count_by_STRICT_post_PROV_O_component_recall: q1
q4_gain_vs_q1: null

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
