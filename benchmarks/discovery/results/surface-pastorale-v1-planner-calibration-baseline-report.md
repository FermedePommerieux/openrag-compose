# Product-path discovery benchmark

benchmark_id: surface-pastorale-v1
runtime_source_sha: 74263a9b812a2eb0a83bc676cd37d3dfe82c0e1a
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
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 76.1% [76.1%, 76.1%] | 2.060 [2.060, 2.060] | 4.468 [3.735, 8.674]s | 10/10 |
| q4 | 26.2% [22.4%, 28.1%] | 35.8% [29.8%, 41.2%] | 49.4% [46.9%, 54.2%] | 45.1% [40.4%, 50.9%] | 68.7% [55.1%, 81.8%] | 2.766 [2.333, 3.333] | 7.156 [5.640, 11.020]s | 9/10 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.7% [26.7%, 26.7%] | 38.0% [38.0%, 38.0%] | 53.8% [53.8%, 53.8%] | 50.7% [50.7%, 50.7%] | 100.0% [100.0%, 100.0%] | 2.060 [2.060, 2.060] | 4.468 [3.735, 8.674]s | 10/10 |
| q4 | 24.7% [22.3%, 26.3%] | 36.5% [32.4%, 40.8%] | 55.0% [52.6%, 58.2%] | 50.2% [47.2%, 54.2%] | 84.6% [71.8%, 98.5%] | 2.766 [2.333, 3.333] | 7.156 [5.640, 11.020]s | 9/10 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| q4 | 10 | 0.568 [0.440, 0.987] | 0.592 [0.410, 0.991] |

## Invalid runs

- q4-r8: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete

## Historical comparison (STRICT)

| q | Metric | Historical | Product mean | Delta |
|---:|---|---:|---:|---:|
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.836 | 4.468 | -0.368 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.731 | 4.468 | +0.737 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.957 | 4.468 | +0.511 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.172 | 4.468 | +0.296 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.705 | 4.468 | +0.763 |
| q4 | seed_component_recall | 35.1% | 35.8% | +0.7% |
| q4 | post_prov_o_component_recall | 47.4% | 45.1% | -2.2% |
| q4 | post_prov_o_document_recall | 52.6% | 49.4% | -3.2% |
| q4 | precision | 70.0% | 68.7% | -1.3% |
| q4 | expansion_per_seed_document | 2.671 | 2.766 | +0.095 |
| q4 | total_latency_seconds | 6.703 | 7.156 | +0.453 |
| q4 | seed_component_recall | 33.3% | 35.8% | +2.4% |
| q4 | post_prov_o_component_recall | 42.1% | 45.1% | +3.0% |
| q4 | post_prov_o_document_recall | 45.8% | 49.4% | +3.5% |
| q4 | precision | 68.7% | 68.7% | +0.1% |
| q4 | expansion_per_seed_document | 3.194 | 2.766 | -0.428 |
| q4 | total_latency_seconds | 7.276 | 7.156 | -0.120 |
| q4 | seed_component_recall | 29.8% | 35.8% | +5.9% |
| q4 | post_prov_o_component_recall | 41.2% | 45.1% | +3.9% |
| q4 | post_prov_o_document_recall | 46.4% | 49.4% | +3.0% |
| q4 | precision | 60.8% | 68.7% | +7.9% |
| q4 | expansion_per_seed_document | 3.216 | 2.766 | -0.450 |
| q4 | total_latency_seconds | 6.874 | 7.156 | +0.283 |
| q4 | seed_component_recall | 35.1% | 35.8% | +0.7% |
| q4 | post_prov_o_component_recall | 44.7% | 45.1% | +0.4% |
| q4 | post_prov_o_document_recall | 49.5% | 49.4% | -0.1% |
| q4 | precision | 58.5% | 68.7% | +10.2% |
| q4 | expansion_per_seed_document | 2.634 | 2.766 | +0.132 |
| q4 | total_latency_seconds | 7.138 | 7.156 | +0.018 |
| q4 | seed_component_recall | 33.3% | 35.8% | +2.4% |
| q4 | post_prov_o_component_recall | 40.4% | 45.1% | +4.8% |
| q4 | post_prov_o_document_recall | 44.8% | 49.4% | +4.6% |
| q4 | precision | 59.0% | 68.7% | +9.7% |
| q4 | expansion_per_seed_document | 2.747 | 2.766 | +0.019 |
| q4 | total_latency_seconds | 6.891 | 7.156 | +0.265 |
| q4 | seed_component_recall | 37.7% | 35.8% | -1.9% |
| q4 | post_prov_o_component_recall | 48.2% | 45.1% | -3.1% |
| q4 | post_prov_o_document_recall | 53.1% | 49.4% | -3.8% |
| q4 | precision | 66.2% | 68.7% | +2.5% |
| q4 | expansion_per_seed_document | 2.875 | 2.766 | -0.109 |
| q4 | total_latency_seconds | 7.129 | 7.156 | +0.027 |
| q4 | seed_component_recall | 35.1% | 35.8% | +0.7% |
| q4 | post_prov_o_component_recall | 43.0% | 45.1% | +2.1% |
| q4 | post_prov_o_document_recall | 47.4% | 49.4% | +2.0% |
| q4 | precision | 59.0% | 68.7% | +9.7% |
| q4 | expansion_per_seed_document | 3.205 | 2.766 | -0.439 |
| q4 | total_latency_seconds | 7.372 | 7.156 | -0.215 |
| q4 | seed_component_recall | 36.0% | 35.8% | -0.2% |
| q4 | post_prov_o_component_recall | 44.7% | 45.1% | +0.4% |
| q4 | post_prov_o_document_recall | 49.0% | 49.4% | +0.4% |
| q4 | precision | 64.5% | 68.7% | +4.2% |
| q4 | expansion_per_seed_document | 2.605 | 2.766 | +0.161 |
| q4 | total_latency_seconds | 6.425 | 7.156 | +0.732 |
| q4 | seed_component_recall | 41.2% | 35.8% | -5.5% |
| q4 | post_prov_o_component_recall | 49.1% | 45.1% | -4.0% |
| q4 | post_prov_o_document_recall | 51.0% | 49.4% | -1.7% |
| q4 | precision | 75.0% | 68.7% | -6.3% |
| q4 | expansion_per_seed_document | 2.653 | 2.766 | +0.113 |
| q4 | total_latency_seconds | 5.809 | 7.156 | +1.348 |
| q4 | seed_component_recall | 36.8% | 35.8% | -1.1% |
| q4 | post_prov_o_component_recall | 45.6% | 45.1% | -0.5% |
| q4 | post_prov_o_document_recall | 48.4% | 49.4% | +0.9% |
| q4 | precision | 70.7% | 68.7% | -1.9% |
| q4 | expansion_per_seed_document | 3.173 | 2.766 | -0.407 |
| q4 | total_latency_seconds | 7.148 | 7.156 | +0.008 |

## Contract audit

all_contracts_valid: false
all_configurations_fully_valid: false
best_query_count_by_STRICT_post_PROV_O_component_recall: q4
q4_gain_vs_q1: {"post_prov_o_component_recall": 0.003898635477582846, "post_prov_o_document_recall": 0.03530092592592593, "seed_component_recall": -0.001949317738791423}

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
