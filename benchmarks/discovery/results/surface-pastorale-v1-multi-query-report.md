## A. Baseline

benchmark_tag: v0.6.0-retrieval-v2-discovery-benchmark-v1
benchmark_sha: 66a1ee9e52ddaee5561b3b38360164ce01bc3927
runtime_baseline: {'llm_provider': 'openai', 'llm_model': 'gpt-5.6-sol', 'embedding_provider': 'openai', 'embedding_model': 'text-embedding-3-large', 'retrieval_strategy': 'rrf', 'retrieval_mode': 'hybrid', 'retrieval_lexical_candidates': 50, 'retrieval_vector_candidates': 50, 'retrieval_rrf_k': 60, 'retrieval_scope_seed_count': 100}
scope_policy: documentary-prov-o v1
embedding: openai / text-embedding-3-large

## B. Architecture

query_generator: bounded structured LLM planner; original query injected
max_queries: 4
query_normalization: NFKD accents + casefold + punctuation + whitespace
retrieval_fanout: lexical+dense per query; concurrency=2
fusion: hierarchical RRF; sum_q(1/(60+per-query-RRF-rank))
final_seed_budget: 96

## C. Generality

domain_specific_terms_in_product_code: 0
ground_truth_accessible_to_query_generator: no
case_specific_logic: none

## D. Tests

multi_query_unit: pass
retrieval: pass
DLS: pass
coverage: pass
agent_guard: pass
benchmark: pass
full_suite_if_needed: pass
Ruff: pass
Mypy: pass
git_diff_check: pass

## E. Benchmark STRICT

| Queries | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Total latency |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 48/192 (25.0%) | 40/114 (35.1%) | 88/192 (45.8%) | 51/114 (44.7%) | 48/63 (76.2%) | 2.19× | 3.637s |
| 2 | 55/192 (28.6%) | 44/114 (38.6%) | 91/192 (47.4%) | 53/114 (46.5%) | 55/69 (79.7%) | 2.16× | 7.814s |
| 3 | 50/192 (26.0%) | 46/114 (40.4%) | 100/192 (52.1%) | 56/114 (49.1%) | 50/64 (78.1%) | 2.59× | 8.723s |
| 4 | 54/192 (28.1%) | 47/114 (41.2%) | 100/192 (52.1%) | 57/114 (50.0%) | 54/66 (81.8%) | 2.45× | 7.977s |

## F. Benchmark BROAD

| Queries | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion | Total latency |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 63/251 (25.1%) | 52/142 (36.6%) | 135/251 (53.8%) | 72/142 (50.7%) | 63/63 (100.0%) | 2.19× | 3.637s |
| 2 | 66/251 (26.3%) | 55/142 (38.7%) | 134/251 (53.4%) | 72/142 (50.7%) | 66/69 (95.7%) | 2.16× | 7.814s |
| 3 | 63/251 (25.1%) | 58/142 (40.8%) | 146/251 (58.2%) | 77/142 (54.2%) | 63/64 (98.4%) | 2.59× | 8.723s |
| 4 | 65/251 (25.9%) | 56/142 (39.4%) | 143/251 (57.0%) | 75/142 (52.8%) | 65/66 (98.5%) | 2.45× | 7.977s |

## G. Marginal query gain

| Added query | New CORE components seeded | Cumulative recall | Duplicate ratio | Added latency |
|---:|---:|---:|---:|---:|
| q0 | 40 | 35.1% | 0.0% | 3.637s |
| q1 | 9 | 38.6% | 24.0% | 4.177s |
| q2 | 7 | 40.4% | 32.6% | 0.909s |
| q3 | 2 | 41.2% | 42.5% | -0.745s |

## H. Misses

CORE components still missed by best configuration: 57
- T007: global seed budget displacement; query={'query_id': 'q0', 'query_type': 'original', 'lexical_rank': None, 'dense_rank': 15, 'query_rrf_rank': 30, 'query_text': 'Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.'}; lexical=None; dense=15; RRF=None; connected
- T030: global seed budget displacement; query={'query_id': 'q0', 'query_type': 'original', 'lexical_rank': 18, 'dense_rank': None, 'query_rrf_rank': 36, 'query_text': 'Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.'}; lexical=18; dense=None; RRF=None; connected
- T032: global seed budget displacement; query={'query_id': 'q0', 'query_type': 'original', 'lexical_rank': 19, 'dense_rank': None, 'query_rrf_rank': 38, 'query_text': 'Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.'}; lexical=19; dense=None; RRF=None; connected
- T041: global seed budget displacement; query={'query_id': 'q0', 'query_type': 'original', 'lexical_rank': None, 'dense_rank': 27, 'query_rrf_rank': 54, 'query_text': 'Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.'}; lexical=None; dense=27; RRF=None; connected
- T042: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- T043: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- T049: global seed budget displacement; query={'query_id': 'q0', 'query_type': 'original', 'lexical_rank': 23, 'dense_rank': None, 'query_rrf_rank': 46, 'query_text': 'Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.'}; lexical=23; dense=None; RRF=None; connected
- S001: global seed budget displacement; query={'query_id': 'q0', 'query_type': 'original', 'lexical_rank': 27, 'dense_rank': None, 'query_rrf_rank': 53, 'query_text': 'Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.'}; lexical=27; dense=None; RRF=None; isolated
- S018: global seed budget displacement; query={'query_id': 'q3', 'query_type': 'administrative_legal', 'lexical_rank': 22, 'dense_rank': None, 'query_rrf_rank': 43, 'query_text': '"Surface pastorale" demandes réponses notifications administratives'}; lexical=22; dense=None; RRF=None; isolated
- CONTROL-CORE-001: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-005: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-006: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-008: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-009: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-010: global seed budget displacement; query={'query_id': 'q3', 'query_type': 'administrative_legal', 'lexical_rank': None, 'dense_rank': 24, 'query_rrf_rank': 47, 'query_text': '"Surface pastorale" demandes réponses notifications administratives'}; lexical=None; dense=24; RRF=None; isolated
- CONTROL-CORE-011: global seed budget displacement; query={'query_id': 'q3', 'query_type': 'administrative_legal', 'lexical_rank': None, 'dense_rank': 23, 'query_rrf_rank': 46, 'query_text': '"Surface pastorale" demandes réponses notifications administratives'}; lexical=None; dense=23; RRF=None; isolated
- CONTROL-CORE-012: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-013: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-014: global seed budget displacement; query={'query_id': 'q1', 'query_type': 'entity_focus', 'lexical_rank': None, 'dense_rank': 35, 'query_rrf_rank': 70, 'query_text': '"Surface pastorale" correspondance avec l’administration'}; lexical=None; dense=35; RRF=None; isolated
- CONTROL-CORE-015: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-016: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-017: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-019: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-020: global seed budget displacement; query={'query_id': 'q1', 'query_type': 'entity_focus', 'lexical_rank': None, 'dense_rank': 18, 'query_rrf_rank': 35, 'query_text': '"Surface pastorale" correspondance avec l’administration'}; lexical=None; dense=18; RRF=None; isolated
- CONTROL-CORE-023: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-024: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 7, 'query_rrf_rank': 18, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=7; RRF=None; connected
- CONTROL-CORE-025: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 13, 'query_rrf_rank': 27, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=13; RRF=None; isolated
- CONTROL-CORE-026: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 14, 'query_rrf_rank': 28, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=14; RRF=None; isolated
- CONTROL-CORE-029: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-030: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-031: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 28, 'query_rrf_rank': 56, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=28; RRF=None; isolated
- CONTROL-CORE-034: global seed budget displacement; query={'query_id': 'q3', 'query_type': 'administrative_legal', 'lexical_rank': 32, 'dense_rank': None, 'query_rrf_rank': 62, 'query_text': '"Surface pastorale" demandes réponses notifications administratives'}; lexical=32; dense=None; RRF=None; isolated
- CONTROL-CORE-035: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-036: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-037: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-038: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-039: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-040: global seed budget displacement; query={'query_id': 'q1', 'query_type': 'entity_focus', 'lexical_rank': None, 'dense_rank': 44, 'query_rrf_rank': 86, 'query_text': '"Surface pastorale" correspondance avec l’administration'}; lexical=None; dense=44; RRF=None; isolated
- CONTROL-CORE-041: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 23, 'query_rrf_rank': 45, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=23; RRF=None; isolated
- CONTROL-CORE-045: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-046: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-048: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-050: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-051: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-052: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-056: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-057: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-058: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 34, 'query_rrf_rank': 68, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=34; RRF=None; connected
- CONTROL-CORE-059: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CORE-060: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-063: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-064: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CORE-066: global seed budget displacement; query={'query_id': 'q2', 'query_type': 'documentary_subject', 'lexical_rank': None, 'dense_rank': 10, 'query_rrf_rank': 24, 'query_text': 'projet "Surface pastorale" courriels lettres comptes rendus échanges'}; lexical=None; dense=10; RRF=None; isolated
- CONTROL-CONTEXT-005: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected
- CONTROL-CONTEXT-006: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-CONTEXT-007: isolated component outside all query horizons; query=None; lexical=None; dense=None; RRF=None; isolated
- CONTROL-NR-001: all generated query lanes missed; query=None; lexical=None; dense=None; RRF=None; connected

## I. Performance

query_generation: 3.946s
retrieval: 1.266s
fusion: 0.006s
PROV-O: 2.766s
total: 7.977s
latency_multiplier_vs_q1: 2.193×

## J. Best configuration

query_count: 4
seed_budget: 96
Seed_Component_Recall: 47/114 (41.2%)
Post_PROV_O_Component_Recall: 57/114 (50.0%)
Precision: 54/66 (81.8%)
Expansion: 2.455×
Latency: 7.977s

## K. Decision

MULTI-QUERY DISCOVERY VALIDATED

## L. Qwen readiness

ready_to_benchmark_qwen: true
reason: The OpenAI multi-query pipeline is stable and isolated from embedding changes.

## M. Production

commit: pending
push: pending
build: pending
gitops: pending
deploy: pending

## N. Conclusion

PHASE 3 GENERIC MULTI-QUERY DISCOVERY VALIDATED
