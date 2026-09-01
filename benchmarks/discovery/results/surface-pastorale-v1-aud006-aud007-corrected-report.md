# Product-path discovery benchmark

benchmark_id: surface-pastorale-v1
runtime_source_sha: 8700bed557db0d7d37906aa1ef2c092960cc09a0
product_endpoint: https://openrag.ferme-de-pommerieux.fr/api/search
DLS_identity: product no-auth identity
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
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 76.1% [76.1%, 76.1%] | 2.060 [2.060, 2.060] | 3.990 [3.682, 4.438]s | 10/10 |
| q4 | 26.5% [24.5%, 28.1%] | 37.6% [34.2%, 41.2%] | 49.7% [47.4%, 53.1%] | 46.7% [43.9%, 50.9%] | 69.1% [65.4%, 74.0%] | 2.723 [2.386, 3.324] | 6.758 [5.974, 7.871]s | 8/10 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.7% [26.7%, 26.7%] | 38.0% [38.0%, 38.0%] | 53.8% [53.8%, 53.8%] | 50.7% [50.7%, 50.7%] | 100.0% [100.0%, 100.0%] | 2.060 [2.060, 2.060] | 3.990 [3.682, 4.438]s | 10/10 |
| q4 | 25.4% [23.9%, 26.7%] | 37.9% [35.2%, 40.8%] | 55.7% [53.8%, 59.0%] | 51.4% [49.3%, 54.9%] | 86.7% [81.5%, 91.8%] | 2.723 [2.386, 3.324] | 6.758 [5.974, 7.871]s | 8/10 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| q4 | 10 | 0.643 [0.550, 0.932] | 0.620 [0.413, 0.938] |

## Invalid runs

- q4-r8: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete
- q4-r10: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete

## Historical comparison (STRICT)

| q | Metric | Historical | Product mean | Delta |
|---:|---|---:|---:|---:|
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.349 | 3.990 | -0.359 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.800 | 3.990 | +0.189 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.330 | 3.990 | -0.340 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.250 | 3.990 | -0.260 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.609 | 3.990 | +0.381 |
| q4 | seed_component_recall | 37.7% | 37.6% | -0.1% |
| q4 | post_prov_o_component_recall | 46.5% | 46.7% | +0.2% |
| q4 | post_prov_o_document_recall | 48.4% | 49.7% | +1.3% |
| q4 | precision | 61.7% | 69.1% | +7.4% |
| q4 | expansion_per_seed_document | 3.086 | 2.723 | -0.363 |
| q4 | total_latency_seconds | 7.095 | 6.758 | -0.337 |
| q4 | seed_component_recall | 34.2% | 37.6% | +3.4% |
| q4 | post_prov_o_component_recall | 43.0% | 46.7% | +3.7% |
| q4 | post_prov_o_document_recall | 46.9% | 49.7% | +2.9% |
| q4 | precision | 70.6% | 69.1% | -1.5% |
| q4 | expansion_per_seed_document | 2.691 | 2.723 | +0.032 |
| q4 | total_latency_seconds | 6.584 | 6.758 | +0.174 |
| q4 | seed_component_recall | 35.1% | 37.6% | +2.5% |
| q4 | post_prov_o_component_recall | 45.6% | 46.7% | +1.1% |
| q4 | post_prov_o_document_recall | 51.6% | 49.7% | -1.8% |
| q4 | precision | 62.8% | 69.1% | +6.3% |
| q4 | expansion_per_seed_document | 2.744 | 2.723 | -0.021 |
| q4 | total_latency_seconds | 6.387 | 6.758 | +0.371 |
| q4 | seed_component_recall | 30.7% | 37.6% | +6.9% |
| q4 | post_prov_o_component_recall | 41.2% | 46.7% | +5.5% |
| q4 | post_prov_o_document_recall | 49.0% | 49.7% | +0.8% |
| q4 | precision | 51.1% | 69.1% | +18.0% |
| q4 | expansion_per_seed_document | 2.614 | 2.723 | +0.109 |
| q4 | total_latency_seconds | 6.861 | 6.758 | -0.104 |
| q4 | seed_component_recall | 39.5% | 37.6% | -1.9% |
| q4 | post_prov_o_component_recall | 50.0% | 46.7% | -3.3% |
| q4 | post_prov_o_document_recall | 51.0% | 49.7% | -1.3% |
| q4 | precision | 66.2% | 69.1% | +2.9% |
| q4 | expansion_per_seed_document | 3.078 | 2.723 | -0.355 |
| q4 | total_latency_seconds | 6.839 | 6.758 | -0.082 |
| q4 | seed_component_recall | 42.1% | 37.6% | -4.5% |
| q4 | post_prov_o_component_recall | 50.9% | 46.7% | -4.2% |
| q4 | post_prov_o_document_recall | 54.7% | 49.7% | -4.9% |
| q4 | precision | 77.3% | 69.1% | -8.2% |
| q4 | expansion_per_seed_document | 2.400 | 2.723 | +0.323 |
| q4 | total_latency_seconds | 7.721 | 6.758 | -0.963 |
| q4 | seed_component_recall | 38.6% | 37.6% | -1.0% |
| q4 | post_prov_o_component_recall | 47.4% | 46.7% | -0.7% |
| q4 | post_prov_o_document_recall | 49.5% | 49.7% | +0.3% |
| q4 | precision | 69.7% | 69.1% | -0.6% |
| q4 | expansion_per_seed_document | 3.224 | 2.723 | -0.501 |
| q4 | total_latency_seconds | 6.396 | 6.758 | +0.362 |
| q4 | seed_component_recall | 35.1% | 37.6% | +2.5% |
| q4 | post_prov_o_component_recall | 43.0% | 46.7% | +3.7% |
| q4 | post_prov_o_document_recall | 42.7% | 49.7% | +7.0% |
| q4 | precision | 78.3% | 69.1% | -9.2% |
| q4 | expansion_per_seed_document | 2.767 | 2.723 | -0.044 |
| q4 | total_latency_seconds | 6.314 | 6.758 | +0.444 |
| q4 | seed_component_recall | 33.3% | 37.6% | +4.3% |
| q4 | post_prov_o_component_recall | 44.7% | 46.7% | +2.0% |
| q4 | post_prov_o_document_recall | 47.9% | 49.7% | +1.8% |
| q4 | precision | 59.7% | 69.1% | +9.4% |
| q4 | expansion_per_seed_document | 3.247 | 2.723 | -0.524 |
| q4 | total_latency_seconds | 7.381 | 6.758 | -0.623 |
| q4 | seed_component_recall | 42.1% | 37.6% | -4.5% |
| q4 | post_prov_o_component_recall | 50.9% | 46.7% | -4.2% |
| q4 | post_prov_o_document_recall | 54.7% | 49.7% | -4.9% |
| q4 | precision | 77.3% | 69.1% | -8.2% |
| q4 | expansion_per_seed_document | 2.400 | 2.723 | +0.323 |
| q4 | total_latency_seconds | 6.248 | 6.758 | +0.509 |

## Contract audit

all_contracts_valid: false
all_configurations_fully_valid: false
best_query_count_by_STRICT_post_PROV_O_component_recall: q4
q4_gain_vs_q1: {"post_prov_o_component_recall": 0.019736842105263164, "post_prov_o_document_recall": 0.0390625, "seed_component_recall": 0.016447368421052655}

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
