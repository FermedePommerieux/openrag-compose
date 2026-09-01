# Product-path discovery benchmark

benchmark_id: orange-fibre-cross-domain-v1
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

## BROAD

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 22.9% [22.9%, 22.9%] | 28.6% [28.6%, 28.6%] | 29.5% [29.5%, 29.5%] | 28.6% [28.6%, 28.6%] | 30.0% [30.0%, 30.0%] | 2.562 [2.562, 2.562] | 3.757 [3.611, 3.885]s | 3/3 |

## STRICT

| q | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Latency | Valid |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| q1 | 45.8% [45.8%, 45.8%] | 66.7% [66.7%, 66.7%] | 60.4% [60.4%, 60.4%] | 66.7% [66.7%, 66.7%] | 27.5% [27.5%, 27.5%] | 2.562 [2.562, 2.562] | 3.757 [3.611, 3.885]s | 3/3 |

## Variance

| q | Query variants | Seed Jaccard | Scope Jaccard |
|---:|---:|---:|---:|
| q1 | 1 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

## Invalid runs

None.

## Contract audit

all_contracts_valid: true
all_configurations_fully_valid: true
best_query_count_by_STRICT_post_PROV_O_component_recall: q1
q4_gain_vs_q1: null

Historical figures remain reference-only because their runner did not use the product endpoint and used a post-hoc seed plateau.
