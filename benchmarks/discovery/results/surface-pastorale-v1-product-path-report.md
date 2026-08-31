# Product-path discovery benchmark

benchmark_id: surface-pastorale-v1
runtime_source_sha: 4bc8200489a4f02ecd629521576fed44a7d889d4
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
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 76.1% [76.1%, 76.1%] | 2.060 [2.060, 2.060] | 4.443 [3.696, 5.020]s | 3/3 |
| q2 | 25.8% [24.0%, 27.6%] | 34.2% [29.8%, 38.6%] | 48.7% [45.3%, 52.1%] | 44.3% [39.5%, 49.1%] | 68.1% [58.2%, 77.9%] | 2.751 [2.709, 2.794] | 5.589 [5.173, 6.005]s | 2/3 |
| q3 | 26.7% [22.9%, 28.6%] | 35.1% [29.8%, 37.7%] | 47.7% [44.3%, 49.5%] | 43.6% [37.7%, 46.5%] | 67.9% [53.0%, 75.3%] | 2.354 [2.205, 2.651] | 5.787 [5.643, 6.062]s | 3/3 |
| q4 | 27.4% [24.0%, 30.2%] | 37.7% [33.3%, 42.1%] | 50.2% [45.8%, 54.7%] | 46.2% [41.2%, 50.9%] | 69.6% [60.5%, 77.3%] | 2.660 [2.400, 2.921] | 6.263 [6.045, 6.683]s | 3/3 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.7% [26.7%, 26.7%] | 38.0% [38.0%, 38.0%] | 53.8% [53.8%, 53.8%] | 50.7% [50.7%, 50.7%] | 100.0% [100.0%, 100.0%] | 2.060 [2.060, 2.060] | 4.443 [3.696, 5.020]s | 3/3 |
| q2 | 25.7% [24.7%, 26.7%] | 36.6% [33.1%, 40.1%] | 55.6% [53.0%, 58.2%] | 50.4% [46.5%, 54.2%] | 88.5% [78.5%, 98.5%] | 2.751 [2.709, 2.794] | 5.589 [5.173, 6.005]s | 2/3 |
| q3 | 25.1% [22.7%, 26.3%] | 36.4% [33.1%, 38.0%] | 52.7% [51.4%, 53.4%] | 49.1% [45.8%, 50.7%] | 83.2% [68.7%, 90.4%] | 2.354 [2.205, 2.651] | 5.787 [5.643, 6.062]s | 3/3 |
| q4 | 26.0% [23.5%, 28.3%] | 38.5% [35.2%, 42.3%] | 55.9% [51.4%, 60.2%] | 51.4% [47.2%, 55.6%] | 86.4% [77.6%, 94.7%] | 2.660 [2.400, 2.921] | 6.263 [6.045, 6.683]s | 3/3 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| q2 | 3 | 0.470 [0.470, 0.470] | 0.485 [0.485, 0.485] |
| q3 | 2 | 0.686 [0.529, 1.000] | 0.743 [0.614, 1.000] |
| q4 | 3 | 0.584 [0.525, 0.659] | 0.610 [0.570, 0.675] |

## Invalid runs

- q2-r2: coverage=document_limit_reached; contract=coverage_incomplete,coverage_status_not_complete,coverage_failure_codes_present,graph_frontier_not_empty,graph_limit_reached

## Historical comparison (STRICT)

| q | Metric | Historical | Product mean | Delta |
|---:|---|---:|---:|---:|
| q1 | seed_component_recall | 35.1% | 36.0% | +0.9% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.2% | 76.1% | -0.1% |
| q1 | expansion_per_seed_document | 2.190 | 2.060 | -0.131 |
| q1 | total_latency_seconds | 3.637 | 4.443 | +0.806 |
| q2 | seed_component_recall | 38.6% | 34.2% | -4.4% |
| q2 | post_prov_o_component_recall | 46.5% | 44.3% | -2.2% |
| q2 | post_prov_o_document_recall | 47.4% | 48.7% | +1.3% |
| q2 | precision | 79.7% | 68.1% | -11.6% |
| q2 | expansion_per_seed_document | 2.159 | 2.751 | +0.592 |
| q2 | total_latency_seconds | 7.814 | 5.589 | -2.225 |
| q3 | seed_component_recall | 40.4% | 35.1% | -5.3% |
| q3 | post_prov_o_component_recall | 49.1% | 43.6% | -5.6% |
| q3 | post_prov_o_document_recall | 52.1% | 47.7% | -4.3% |
| q3 | precision | 78.1% | 67.9% | -10.2% |
| q3 | expansion_per_seed_document | 2.594 | 2.354 | -0.240 |
| q3 | total_latency_seconds | 8.723 | 5.787 | -2.936 |
| q4 | seed_component_recall | 41.2% | 37.7% | -3.5% |
| q4 | post_prov_o_component_recall | 50.0% | 46.2% | -3.8% |
| q4 | post_prov_o_document_recall | 52.1% | 50.2% | -1.9% |
| q4 | precision | 81.8% | 69.6% | -12.2% |
| q4 | expansion_per_seed_document | 2.455 | 2.660 | +0.205 |
| q4 | total_latency_seconds | 7.977 | 6.263 | -1.715 |

## Contract audit

all_contracts_valid: false
all_configurations_fully_valid: false
best_query_count_by_STRICT_post_PROV_O_component_recall: q4
q4_gain_vs_q1: {"post_prov_o_component_recall": 0.0146198830409357, "post_prov_o_document_recall": 0.043402777777777735, "seed_component_recall": 0.017543859649122806}

Historical figures remain reference-only because their runner did not use the product endpoint and used a 96-seed plateau.
