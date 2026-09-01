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
| q4 | 24.5% [21.9%, 27.1%] | 33.1% [28.9%, 38.6%] | 46.2% [42.2%, 49.5%] | 41.6% [36.0%, 47.4%] | 63.1% [56.0%, 72.7%] | 2.776 [2.500, 3.240] | 7.007 [6.179, 9.346]s | 9/10 |

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q4 | 23.6% [21.5%, 25.9%] | 34.5% [31.0%, 38.7%] | 53.1% [49.8%, 55.8%] | 47.6% [43.0%, 52.1%] | 79.3% [72.0%, 87.9%] | 2.776 [2.500, 3.240] | 7.007 [6.179, 9.346]s | 9/10 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q4 | 9 | 0.571 [0.439, 1.000] | 0.567 [0.399, 1.000] |

## Invalid runs

- q4-r10: coverage=document_limit_reached; contract=coverage_failure_codes_present,coverage_incomplete,coverage_status_not_complete,graph_frontier_not_empty,graph_limit_reached,validation_failed:coverage_complete

## Contract audit

all_contracts_valid: false
all_configurations_fully_valid: false
best_query_count_by_STRICT_post_PROV_O_component_recall: q4
q4_gain_vs_q1: null

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
