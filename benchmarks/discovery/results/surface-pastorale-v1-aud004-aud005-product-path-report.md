# Product-path discovery benchmark

benchmark_id: surface-pastorale-v1
runtime_source_sha: 8009aacd1d9315c23a715e2120f0c958f1ba573a
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
| q1 | 26.6% [26.6%, 26.6%] | 36.0% [36.0%, 36.0%] | 45.8% [45.8%, 45.8%] | 44.7% [44.7%, 44.7%] | 76.1% [76.1%, 76.1%] | 2.060 [2.060, 2.060] | 4.068 [3.609, 4.349]s | 5/5 |
| q4 | 26.6% [23.4%, 30.2%] | 37.2% [30.7%, 42.1%] | 50.0% [42.7%, 54.7%] | 46.5% [41.2%, 50.9%] | 69.2% [51.1%, 78.3%] | 2.740 [2.400, 3.224] | 6.669 [6.248, 7.721]s | 8/10 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 26.7% [26.7%, 26.7%] | 38.0% [38.0%, 38.0%] | 53.8% [53.8%, 53.8%] | 50.7% [50.7%, 50.7%] | 100.0% [100.0%, 100.0%] | 2.060 [2.060, 2.060] | 4.068 [3.609, 4.349]s | 5/5 |
| q4 | 24.9% [22.3%, 28.3%] | 37.7% [32.4%, 42.3%] | 55.4% [48.6%, 60.2%] | 51.5% [47.2%, 55.6%] | 84.6% [63.6%, 95.0%] | 2.740 [2.400, 3.224] | 6.669 [6.248, 7.721]s | 8/10 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| q4 | 9 | 0.577 [0.333, 1.000] | 0.581 [0.387, 1.000] |

## Invalid runs

- q4-r1: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete
- q4-r9: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete

## Historical comparison (STRICT)

| q | Metric | Historical | Product mean | Delta |
|---:|---|---:|---:|---:|
| q1 | seed_component_recall | 35.1% | 36.0% | +0.9% |
| q1 | post_prov_o_component_recall | 44.7% | 44.7% | +0.0% |
| q1 | post_prov_o_document_recall | 45.8% | 45.8% | +0.0% |
| q1 | precision | 76.2% | 76.1% | -0.1% |
| q1 | expansion_per_seed_document | 2.190 | 2.060 | -0.131 |
| q1 | total_latency_seconds | 3.637 | 4.068 | +0.431 |
| q4 | seed_component_recall | 41.2% | 37.2% | -4.1% |
| q4 | post_prov_o_component_recall | 50.0% | 46.5% | -3.5% |
| q4 | post_prov_o_document_recall | 52.1% | 50.0% | -2.1% |
| q4 | precision | 81.8% | 69.2% | -12.6% |
| q4 | expansion_per_seed_document | 2.455 | 2.740 | +0.285 |
| q4 | total_latency_seconds | 7.977 | 6.669 | -1.309 |

## Contract audit

all_contracts_valid: false
all_configurations_fully_valid: false
best_query_count_by_STRICT_post_PROV_O_component_recall: q4
q4_gain_vs_q1: {"post_prov_o_component_recall": 0.017543859649122806, "post_prov_o_document_recall": 0.041666666666666685, "seed_component_recall": 0.01206140350877194}

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
