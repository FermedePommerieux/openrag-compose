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
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 76.1% [76.1%, 76.1%] | 2.060 [2.060, 2.060] | 4.080 [3.705, 4.836]s | 5/5 |
| q4 | 25.8% [23.4%, 28.1%] | 35.4% [29.8%, 41.2%] | 49.0% [44.8%, 53.1%] | 44.8% [40.4%, 49.1%] | 65.9% [58.5%, 75.0%] | 2.863 [2.605, 3.216] | 6.821 [5.809, 7.276]s | 9/10 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.7% [26.7%, 26.7%] | 38.0% [38.0%, 38.0%] | 53.8% [53.8%, 53.8%] | 50.7% [50.7%, 50.7%] | 100.0% [100.0%, 100.0%] | 2.060 [2.060, 2.060] | 4.080 [3.705, 4.836]s | 5/5 |
| q4 | 24.7% [22.3%, 26.3%] | 36.3% [31.0%, 39.4%] | 54.9% [51.0%, 58.6%] | 50.1% [46.5%, 52.8%] | 82.6% [73.5%, 91.7%] | 2.863 [2.605, 3.216] | 6.821 [5.809, 7.276]s | 9/10 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| q4 | 10 | 0.533 [0.414, 0.793] | 0.534 [0.371, 0.759] |

## Invalid runs

- q4-r7: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete

## Historical comparison (STRICT)

| q | Metric | Historical | Product mean | Delta |
|---:|---|---:|---:|---:|
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.438 | 4.080 | -0.358 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.900 | 4.080 | +0.180 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.057 | 4.080 | +0.023 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.897 | 4.080 | +0.183 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.929 | 4.080 | +0.151 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.966 | 4.080 | +0.114 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.015 | 4.080 | +0.065 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.682 | 4.080 | +0.398 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 3.978 | 4.080 | +0.102 |
| q1 | seed_component_recall | 36.0% | 36.0% | +0.0% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.1% | 76.1% | +0.0% |
| q1 | expansion_per_seed_document | 2.060 | 2.060 | +0.000 |
| q1 | total_latency_seconds | 4.037 | 4.080 | +0.043 |
| q4 | seed_component_recall | 36.0% | 35.4% | -0.6% |
| q4 | post_prov_o_component_recall | 45.6% | 44.8% | -0.8% |
| q4 | post_prov_o_document_recall | 49.5% | 49.0% | -0.5% |
| q4 | precision | 68.5% | 65.9% | -2.6% |
| q4 | expansion_per_seed_document | 2.411 | 2.863 | +0.452 |
| q4 | total_latency_seconds | 6.194 | 6.821 | +0.628 |
| q4 | seed_component_recall | 34.2% | 35.4% | +1.2% |
| q4 | post_prov_o_component_recall | 44.7% | 44.8% | +0.1% |
| q4 | post_prov_o_document_recall | 49.0% | 49.0% | +0.0% |
| q4 | precision | 67.1% | 65.9% | -1.2% |
| q4 | expansion_per_seed_document | 2.386 | 2.863 | +0.478 |
| q4 | total_latency_seconds | 6.270 | 6.821 | +0.552 |
| q4 | seed_component_recall | 39.5% | 35.4% | -4.1% |
| q4 | post_prov_o_component_recall | 47.4% | 44.8% | -2.5% |
| q4 | post_prov_o_document_recall | 49.5% | 49.0% | -0.5% |
| q4 | precision | 70.3% | 65.9% | -4.3% |
| q4 | expansion_per_seed_document | 2.919 | 2.863 | -0.056 |
| q4 | total_latency_seconds | 5.974 | 6.821 | +0.848 |
| q4 | seed_component_recall | 37.7% | 35.4% | -2.3% |
| q4 | post_prov_o_component_recall | 47.4% | 44.8% | -2.5% |
| q4 | post_prov_o_document_recall | 50.5% | 49.0% | -1.6% |
| q4 | precision | 65.4% | 65.9% | +0.5% |
| q4 | expansion_per_seed_document | 2.914 | 2.863 | -0.050 |
| q4 | total_latency_seconds | 7.323 | 6.821 | -0.501 |
| q4 | seed_component_recall | 41.2% | 35.4% | -5.8% |
| q4 | post_prov_o_component_recall | 50.9% | 44.8% | -6.0% |
| q4 | post_prov_o_document_recall | 53.1% | 49.0% | -4.2% |
| q4 | precision | 72.6% | 65.9% | -6.7% |
| q4 | expansion_per_seed_document | 2.507 | 2.863 | +0.356 |
| q4 | total_latency_seconds | 6.373 | 6.821 | +0.449 |
| q4 | seed_component_recall | 41.2% | 35.4% | -5.8% |
| q4 | post_prov_o_component_recall | 49.1% | 44.8% | -4.3% |
| q4 | post_prov_o_document_recall | 51.0% | 49.0% | -2.1% |
| q4 | precision | 74.0% | 65.9% | -8.0% |
| q4 | expansion_per_seed_document | 2.521 | 2.863 | +0.343 |
| q4 | total_latency_seconds | 6.856 | 6.821 | -0.034 |
| q4 | seed_component_recall | 36.0% | 35.4% | -0.6% |
| q4 | post_prov_o_component_recall | 44.7% | 44.8% | +0.1% |
| q4 | post_prov_o_document_recall | 47.9% | 49.0% | +1.0% |
| q4 | precision | 67.6% | 65.9% | -1.6% |
| q4 | expansion_per_seed_document | 3.324 | 2.863 | -0.461 |
| q4 | total_latency_seconds | 7.201 | 6.821 | -0.380 |
| q4 | seed_component_recall | 37.7% | 35.4% | -2.3% |
| q4 | post_prov_o_component_recall | 50.0% | 44.8% | -5.2% |
| q4 | post_prov_o_document_recall | 54.2% | 49.0% | -5.2% |
| q4 | precision | 64.6% | 65.9% | +1.3% |
| q4 | expansion_per_seed_document | 3.049 | 2.863 | -0.186 |
| q4 | total_latency_seconds | 7.485 | 6.821 | -0.664 |
| q4 | seed_component_recall | 35.1% | 35.4% | +0.3% |
| q4 | post_prov_o_component_recall | 43.9% | 44.8% | +1.0% |
| q4 | post_prov_o_document_recall | 47.4% | 49.0% | +1.6% |
| q4 | precision | 67.6% | 65.9% | -1.7% |
| q4 | expansion_per_seed_document | 2.803 | 2.863 | +0.060 |
| q4 | total_latency_seconds | 7.871 | 6.821 | -1.049 |
| q4 | seed_component_recall | 33.3% | 35.4% | +2.0% |
| q4 | post_prov_o_component_recall | 42.1% | 44.8% | +2.7% |
| q4 | post_prov_o_document_recall | 45.8% | 49.0% | +3.1% |
| q4 | precision | 57.9% | 65.9% | +8.0% |
| q4 | expansion_per_seed_document | 3.289 | 2.863 | -0.426 |
| q4 | total_latency_seconds | 7.192 | 6.821 | -0.371 |

## Contract audit

all_contracts_valid: false
all_configurations_fully_valid: false
best_query_count_by_STRICT_post_PROV_O_component_recall: q4
q4_gain_vs_q1: {"post_prov_o_component_recall": 0.0009746588693957392, "post_prov_o_document_recall": 0.03125, "seed_component_recall": -0.005847953216374269}

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
