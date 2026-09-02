# Product-path discovery benchmark

benchmark_id: surface-pastorale-v1
runtime_source_sha: bfbf3622b84deb234709a5c991b3bbbc51ab4bc7
product_endpoint: https://openrag.ferme-de-pommerieux.fr/api/search
DLS_identity: anonymous
global_seed_budget: 100

Validation context: the full 47,400-document metadata backfill completed with observational fields only; chunk text, embedding values, and graph edges were not mutated.

## Corpus

visible_occurrences: 47454
distinct_documents: 47400
digest_before: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
digest_after: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
comparable: true

## STRICT

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 77.3% [77.3%, 77.3%] | 2.061 [2.061, 2.061] | 4.384 [3.870, 4.988]s | 3/3 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.3% [26.3%, 26.3%] | 37.3% [37.3%, 37.3%] | 53.0% [53.0%, 53.0%] | 50.0% [50.0%, 50.0%] | 100.0% [100.0%, 100.0%] | 2.061 [2.061, 2.061] | 4.384 [3.870, 4.988]s | 3/3 |

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
| q1 | precision | 76.1% | 77.3% | +1.2% |
| q1 | expansion_per_seed_document | 2.060 | 2.061 | +0.001 |
| q1 | total_latency_seconds | 4.813 | 4.384 | -0.429 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 77.3% | +1.2% |
| q1 | expansion_per_seed_document | 2.060 | 2.061 | +0.001 |
| q1 | total_latency_seconds | 4.528 | 4.384 | -0.144 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 77.3% | +1.2% |
| q1 | expansion_per_seed_document | 2.060 | 2.061 | +0.001 |
| q1 | total_latency_seconds | 3.963 | 4.384 | +0.420 |

## Contract audit

all_contracts_valid: true
all_configurations_fully_valid: true
best_query_count_by_STRICT_post_PROV_O_component_recall: q1
q4_gain_vs_q1: null

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
