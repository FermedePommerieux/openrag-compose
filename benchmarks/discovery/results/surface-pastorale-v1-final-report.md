## A. Ground truth

benchmark_case: 1
source_sha: 477092776baaacfc9fb6131766e83b32f60b181d
tag: v0.6.0-retrieval-v2-prov-o-scope-policy-v1
scope_policy: documentary-prov-o v1
query_actually_executed: Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.

CORE_components: 114
CONTEXTUAL_components: 28
NOT_RELEVANT_components: 5

CORE_documents: 192
CONTEXTUAL_documents: 59
NOT_RELEVANT_documents: 11

Human validation pipeline:
- initial candidates reviewed: 138
- independent control candidates reviewed: 74
- control decisions: {"CORE": 63, "CONTEXTUAL": 7, "NOT_RELEVANT": 4, "UNREVIEWED": 0}
- remaining review items: 0

Historical runtime distinction: the discovery figures measure the documentary engine at the scope-policy baseline SHA. The later agent-guard tag is preserved unchanged and does not alter these retrieval measurements.

## B. Corpus

visible_occurrences: 47454
distinct_documents: 47400
corpus_digest_before: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
corpus_digest_after: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
comparable: true

## C. STRICT benchmark

| Mode | K | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion |
|---|---:|---:|---:|---:|---:|---:|---:|
| lexical | 20 | 9/192 (4.7%) | 6/114 (5.3%) | 26/192 (13.5%) | 13/114 (11.4%) | 9/16 (56.2%) | 4.12× |
| lexical | 50 | 28/192 (14.6%) | 25/114 (21.9%) | 45/192 (23.4%) | 32/114 (28.1%) | 28/37 (75.7%) | 2.35× |
| lexical | 100 | 28/192 (14.6%) | 25/114 (21.9%) | 45/192 (23.4%) | 32/114 (28.1%) | 28/37 (75.7%) | 2.35× |
| lexical | 200 | 28/192 (14.6%) | 25/114 (21.9%) | 45/192 (23.4%) | 32/114 (28.1%) | 28/37 (75.7%) | 2.35× |
| dense | 20 | 13/192 (6.8%) | 9/114 (7.9%) | 31/192 (16.1%) | 10/114 (8.8%) | 13/13 (100.0%) | 2.92× |
| dense | 50 | 22/192 (11.5%) | 17/114 (14.9%) | 49/192 (25.5%) | 21/114 (18.4%) | 22/31 (71.0%) | 2.45× |
| dense | 100 | 22/192 (11.5%) | 17/114 (14.9%) | 49/192 (25.5%) | 21/114 (18.4%) | 22/31 (71.0%) | 2.45× |
| dense | 200 | 22/192 (11.5%) | 17/114 (14.9%) | 49/192 (25.5%) | 21/114 (18.4%) | 22/31 (71.0%) | 2.45× |
| rrf | 20 | 11/192 (5.7%) | 9/114 (7.9%) | 32/192 (16.7%) | 10/114 (8.8%) | 11/18 (61.1%) | 3.17× |
| rrf | 50 | 24/192 (12.5%) | 17/114 (14.9%) | 60/192 (31.2%) | 26/114 (22.8%) | 24/33 (72.7%) | 3.15× |
| rrf | 100 | 48/192 (25.0%) | 40/114 (35.1%) | 88/192 (45.8%) | 51/114 (44.7%) | 48/63 (76.2%) | 2.19× |
| rrf | 200 | 48/192 (25.0%) | 40/114 (35.1%) | 88/192 (45.8%) | 51/114 (44.7%) | 48/63 (76.2%) | 2.19× |

## D. BROAD benchmark

| Mode | K | Seed Doc Recall | Seed Component Recall | Post-PROV-O Doc Recall | Post-PROV-O Component Recall | Precision | Expansion |
|---|---:|---:|---:|---:|---:|---:|---:|
| lexical | 20 | 16/251 (6.4%) | 15/142 (10.6%) | 64/251 (25.5%) | 31/142 (21.8%) | 16/16 (100.0%) | 4.12× |
| lexical | 50 | 37/251 (14.7%) | 35/142 (24.6%) | 85/251 (33.9%) | 51/142 (35.9%) | 37/37 (100.0%) | 2.35× |
| lexical | 100 | 37/251 (14.7%) | 35/142 (24.6%) | 85/251 (33.9%) | 51/142 (35.9%) | 37/37 (100.0%) | 2.35× |
| lexical | 200 | 37/251 (14.7%) | 35/142 (24.6%) | 85/251 (33.9%) | 51/142 (35.9%) | 37/37 (100.0%) | 2.35× |
| dense | 20 | 13/251 (5.2%) | 10/142 (7.0%) | 38/251 (15.1%) | 12/142 (8.5%) | 13/13 (100.0%) | 2.92× |
| dense | 50 | 31/251 (12.4%) | 22/142 (15.5%) | 73/251 (29.1%) | 29/142 (20.4%) | 31/31 (100.0%) | 2.45× |
| dense | 100 | 31/251 (12.4%) | 22/142 (15.5%) | 73/251 (29.1%) | 29/142 (20.4%) | 31/31 (100.0%) | 2.45× |
| dense | 200 | 31/251 (12.4%) | 22/142 (15.5%) | 73/251 (29.1%) | 29/142 (20.4%) | 31/31 (100.0%) | 2.45× |
| rrf | 20 | 18/251 (7.2%) | 17/142 (12.0%) | 55/251 (21.9%) | 19/142 (13.4%) | 18/18 (100.0%) | 3.17× |
| rrf | 50 | 33/251 (13.1%) | 27/142 (19.0%) | 102/251 (40.6%) | 44/142 (31.0%) | 33/33 (100.0%) | 3.15× |
| rrf | 100 | 63/251 (25.1%) | 52/142 (36.6%) | 135/251 (53.8%) | 72/142 (50.7%) | 63/63 (100.0%) | 2.19× |
| rrf | 200 | 63/251 (25.1%) | 52/142 (36.6%) | 135/251 (53.8%) | 72/142 (50.7%) | 63/63 (100.0%) | 2.19× |

K supérieur au nombre de chunks produits par les horizons gelés 50+50 utilise exactement tous les résultats disponibles; `effective_seed_chunks` est conservé dans le JSON/CSV.

## E. Lane contribution

| K | Lexical-only components | Dense-only components | Both | RRF reached | Missed |
|---:|---:|---:|---:|---:|---:|
| 20 | 6 | 9 | 0 | 9 | 99 |
| 50 | 23 | 15 | 2 | 17 | 74 |
| 100 | 23 | 15 | 2 | 40 | 74 |
| 200 | 23 | 15 | 2 | 40 | 74 |

Composantes STRICT ratées par toutes les lanes à K=200 : CONTROL-CONTEXT-005, CONTROL-CONTEXT-006, CONTROL-CONTEXT-007, CONTROL-CORE-001, CONTROL-CORE-002, CONTROL-CORE-003, CONTROL-CORE-004, CONTROL-CORE-005, CONTROL-CORE-006, CONTROL-CORE-008, CONTROL-CORE-009, CONTROL-CORE-010, CONTROL-CORE-011, CONTROL-CORE-012, CONTROL-CORE-013, CONTROL-CORE-014, CONTROL-CORE-015, CONTROL-CORE-016, CONTROL-CORE-017, CONTROL-CORE-018, CONTROL-CORE-019, CONTROL-CORE-020, CONTROL-CORE-021, CONTROL-CORE-022, CONTROL-CORE-023, CONTROL-CORE-024, CONTROL-CORE-025, CONTROL-CORE-026, CONTROL-CORE-027, CONTROL-CORE-028, CONTROL-CORE-029, CONTROL-CORE-030, CONTROL-CORE-031, CONTROL-CORE-032, CONTROL-CORE-033, CONTROL-CORE-034, CONTROL-CORE-035, CONTROL-CORE-036, CONTROL-CORE-037, CONTROL-CORE-038, CONTROL-CORE-039, CONTROL-CORE-040, CONTROL-CORE-041, CONTROL-CORE-042, CONTROL-CORE-043, CONTROL-CORE-044, CONTROL-CORE-045, CONTROL-CORE-046, CONTROL-CORE-048, CONTROL-CORE-050, CONTROL-CORE-051, CONTROL-CORE-052, CONTROL-CORE-053, CONTROL-CORE-055, CONTROL-CORE-056, CONTROL-CORE-057, CONTROL-CORE-058, CONTROL-CORE-059, CONTROL-CORE-060, CONTROL-CORE-063, CONTROL-CORE-064, CONTROL-CORE-066, CONTROL-NR-001, T004, T008, T009, T010, T015, T016, T017, T018, T029, T042, T043

## F. PROV-O recovery

recovery_gain_by_K:
- K=20: 21
- K=50: 36
- K=100: 40
- K=200: 40
recovery_multiplier_by_K:
- K=20: 2.909×
- K=50: 2.500×
- K=100: 1.833×
- K=200: 1.833×

`coverage.complete=true` certifies complete closure of the documentary scope actually seeded under the declared policy. It does not certify that every relevant corpus component received a seed.

## G. Miss analysis

- CONTROL-CORE-001: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:0baa0baa-55fb-4746-8fad-47e166530047, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:e61b5c12-ae79-479b-b03c-6ef3b13fc159
- CONTROL-CORE-002: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:d94c769e-2f3d-468d-8860-5027b8b1f45a
- CONTROL-CORE-003: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:7f04a597-615d-42d9-9dfb-0100f5eff8df
- CONTROL-CORE-004: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:05ead953-2dfb-4ac8-9cb1-bea9b522795f
- CONTROL-CORE-005: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:498bf3655e44be8e785a10e001e7331a3652781fefaa7810a0e5fbb7372d5603
- CONTROL-CORE-006: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:6af06061-33c5-4334-9e9c-2f02e3e74c34
- CONTROL-CORE-008: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:542c28c4-00f2-4549-9ad5-ca99409b3e71, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a7688696-c2f0-43b7-b643-e2ec48f709b9
- CONTROL-CORE-009: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:6c4c2559-b0a8-43c4-8f18-26f889fe74dc
- CONTROL-CORE-010: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:142d86de-c489-44d3-afac-2931aeb39356
- CONTROL-CORE-011: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:2d097198-fb50-4aed-a60f-71e090d94158
- CONTROL-CORE-012: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:cbca690b4d2b0ea197722e24a51ffb59655c57571409392b748686aa78a39f25
- CONTROL-CORE-013: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:8959dbe9-c4db-4166-9182-18ba4fb18577
- CONTROL-CORE-014: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:f56f98cc-ab97-4c42-9da3-63a3898b4fbb
- CONTROL-CORE-015: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:273e8c08-442e-4052-aa94-5833fc2ab370
- CONTROL-CORE-016: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:2e1e58fd-4357-4fa1-804f-0e9739441f95
- CONTROL-CORE-017: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:4b94e86a-d626-4adf-b8ba-d2356e031dab, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:dd2957fe-e14f-4893-a9a2-afea1c058daa
- CONTROL-CORE-018: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:bf52d20e-f0a3-4d04-bf10-6ea2a2b2c54e
- CONTROL-CORE-019: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a0895133-a4df-4b0b-9235-5760672c1a00
- CONTROL-CORE-020: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:04cc08ca-de86-4539-84d8-9c0ce006cb78
- CONTROL-CORE-021: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:46ba1b56-ad0c-44e0-9513-9f0a6b58351a, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:9091066e-7038-49ff-8c79-9bdd0b216bea, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:ced75d7f-f768-4d5c-9fa8-1ac9df793103
- CONTROL-CORE-022: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:29a44a4e-9c16-443a-a275-4bc54d6d4a71
- CONTROL-CORE-023: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:ea8ccce9-caae-4dea-a052-e7dc06f2687b
- CONTROL-CORE-024: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:858f7482-73c2-428c-9085-0c87f3e0dddc, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a270dcac-8260-4244-8f6d-7de4ddf94ff7
- CONTROL-CORE-025: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:00a595c6-fdba-409e-8837-fa3c67cee87b
- CONTROL-CORE-026: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:6991334c-4985-441a-92d3-03eecb97ba54
- CONTROL-CORE-027: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:900b0cdd-494d-4406-9a11-e5edb5126076
- CONTROL-CORE-028: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:dc3c05c0-e961-4060-b38c-2444aa97e006
- CONTROL-CORE-029: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:6ca4c5b5-3440-4c50-8666-cecfa25916d5:9f24a863-3df3-403f-be11-78a4e4e91a21
- CONTROL-CORE-030: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:ef9ae270-bec1-47b1-be73-5a70ba1ad35c
- CONTROL-CORE-031: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:5276de8f-696c-4e0c-bf1f-b2445567a848
- CONTROL-CORE-032: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:323837f9-e64a-4680-9984-b7a4448a36f3, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:1fb66a35-187b-4b4c-8fb1-79daa8ae4288, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:24479cef-b01d-4b7a-91b6-97a90201e294, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:263e492b-ced2-4f3f-b739-d4c26f29896d, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:5f6f9b86-e964-47e4-86ad-feb8e1a5c888, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:83ceb93b-4572-41e8-b646-83ec85ed3137
- CONTROL-CORE-033: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:b4f388cdd751922f2d50782b87eac4c07af250b3050f322b15a9297220c94e41
- CONTROL-CORE-034: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:4603c6e489f227e19952f568356a50a27ab15074319e55ed25c6d5d1ef5f8a41
- CONTROL-CORE-035: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:21c85031-e540-46ac-9e22-15260b5dc677, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:d8020411-75de-4b58-ad93-3397210ef638
- CONTROL-CORE-036: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:3c993a2bbd8b74b409d7164b17fb5b175ecd25a4f996dc904a98598347707ba7
- CONTROL-CORE-037: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:77450af35a2dc64a5d635d3088e18915e6477c76f49539a082bd4e7b1d88ff25
- CONTROL-CORE-038: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:02671c207e52e670959731a09129ea43c39c369044b82a952fc298bf93fb1efa
- CONTROL-CORE-039: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:add150d039de7cf661cd3ca45d9c1c5d76b7d2666f4487b0be2d5df006dd4819
- CONTROL-CORE-040: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:40c6ef3f66ff0cb284d00f17ef535d81490c57a7e7d02e091f6d88f73958a1ca
- CONTROL-CORE-041: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:08caf12c-d017-40ad-97ae-16f74bf5b309
- CONTROL-CORE-042: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:ecab960b-a319-44c9-b368-154bd0a98ca0
- CONTROL-CORE-043: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:c361af7c-a760-4875-9053-3f14012747e7
- CONTROL-CORE-044: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:f1088f69-53d2-40b1-82b1-23b7c39d908c
- CONTROL-CORE-045: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:local-file:cdb4ee2aea69cc6a83331bbe96dc2caa9a299d21329efb0336fc02a82e1839a8:22f31724b30bb10759d697660749a20d4f66d3eb827d4801579a0f3268df1783
- CONTROL-CORE-046: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:0bde74c0-cb13-4c9e-863d-b5f39f951dc8, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:8c65ea0a-d64d-4556-894d-f62400cc6580
- CONTROL-CORE-048: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:65b69bb0-b405-48dd-9245-bcf4a3574fd7
- CONTROL-CORE-050: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:2a70bc9b-b7e2-4515-aa6b-fac30acfb8f2
- CONTROL-CORE-051: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:690bb9ff-da9c-4d87-8b19-42fc65cfef05, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:785ecc43-2a52-4d5c-9f16-037ff53ab960, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:8b23ba36-910c-4d69-a857-26e263bf75f6, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:c0e0f7d9-05ed-4d01-b4b4-2153d3497eb3
- CONTROL-CORE-052: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:0422bc9d-aa58-425b-a949-d1bf7c13acd2, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:6694af05-49e8-45c5-8c63-de7bbde8be17, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:69154a35-b94c-4a55-aab2-9e042fb766a9, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:6a5140fb-ce42-442d-b79f-575ac920a88a, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:bc643480-cf74-497e-886f-8c7d15c39f21, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:ca7ef965-d66d-43ec-b90a-ef29af5ca12c, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:fb4f956e-c8b2-4b24-a795-15d52c6fb292
- CONTROL-CORE-053: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:e51cd701-dc22-49db-9197-cb49c3ab1b06, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:f2738cfb-decb-48cc-81f7-19a46be92887, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:f8c5a53e-e213-41d1-8bd1-36213f55133d
- CONTROL-CORE-055: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:6ca4c5b5-3440-4c50-8666-cecfa25916d5:937d837c-8ed0-449d-9cc3-3823d684be60
- CONTROL-CORE-056: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a1088c2f-08b8-41cc-9470-75a34bd93baf, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:b7e81c40-eb59-4bf0-aa8a-5f232db5a143
- CONTROL-CORE-057: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:18a84c15-1c77-44bb-8d39-d407f0ae35d6, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:5dd1f658-5064-4e18-8189-1f8354d1e6d9, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:8bfe0fcd-d04c-434f-a5f0-7e9248b136be, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:f0ec7d59-9815-4f6e-8ae3-c50ce3ab61c8
- CONTROL-CORE-058: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:42246f8b-f5f8-424a-8897-0f2be4f30c94, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:7987a272-bda9-4771-969f-5162084bde1d, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a17e2035-4464-4ca2-8ff2-1f2b0e488476, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a492d24a-ceeb-4219-93f2-7927a0e9051c, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:e570912e-f9a7-412c-8944-5e27c8c81839
- CONTROL-CORE-059: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:5189e7d3-48d5-4c36-ac71-548986fc5430, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:94e10ac0-cd29-46ea-a3f2-8e1c51bfc14f
- CONTROL-CORE-060: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:23e554a9-6bd5-48c0-b9a8-8ad716e43d4f
- CONTROL-CORE-063: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:eb8ee247-0cb1-42f0-a35b-873eeea4eae0
- CONTROL-CORE-064: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:cb1feb57-150c-4422-957f-b1c0039a64d9
- CONTROL-CORE-066: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:35edb992-c4a6-45db-b207-eeb3aeba1425
- CONTROL-CONTEXT-005: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:3e039f7b-cdb1-476a-be62-67799e786252, urn:openrag:openarchiver:attachment:42b98794-1084-4da4-8f2f-8bffccfbec3c, urn:openrag:openarchiver:attachment:67f24b4c-9df2-44df-ab7b-49ba46c87949, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:0dc9b2c4-1c8f-42cd-9baf-e21a68c00d7f, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:4a1e126c-ff90-4e6a-966f-d83dd55a863c, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:a9dfc7ae-6326-4c8c-b877-566e2799151f
- CONTROL-CONTEXT-006: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:1e7f1cb3-940a-4e4c-a947-b68b6e6a26e6
- CONTROL-CONTEXT-007: isolated component; lexical=None, dense=None, RRF=None; isolated; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:0da9f94b-1220-407a-b5cb-1ac9c51a0ad4
- CONTROL-NR-001: both lanes miss; lexical=None, dense=None, RRF=None; connected; outside K200=not_observable_under_frozen_50_plus_50_candidate_horizons; documents=urn:openrag:openarchiver:attachment:5fbb6edd-5a7a-4630-8e35-11916ea1056d, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:024e97f6-2694-4bde-98be-2058a03c7322, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:12e524fa-5d1e-4cdb-9c2d-1426a4be40f4, urn:openrag:openarchiver:email:9e325a3e-b01f-40bf-8890-b400d4e3d7f4:b85fd6b0-2515-452f-8e86-46a3f305e4ed

## H. Performance

| Mode | K | Discovery | Scope closure | Total |
|---|---:|---:|---:|---:|
| lexical | 20 | 0.331s | 1.653s | 1.984s |
| lexical | 50 | 0.331s | 2.591s | 2.923s |
| lexical | 100 | 0.331s | 2.591s | 2.923s |
| lexical | 200 | 0.331s | 2.591s | 2.923s |
| dense | 20 | 1.835s | 0.625s | 2.460s |
| dense | 50 | 1.835s | 1.230s | 3.064s |
| dense | 100 | 1.835s | 1.230s | 3.064s |
| dense | 200 | 1.835s | 1.230s | 3.064s |
| rrf | 20 | 2.013s | 1.506s | 3.519s |
| rrf | 50 | 2.013s | 2.226s | 4.240s |
| rrf | 100 | 2.013s | 3.273s | 5.286s |
| rrf | 200 | 2.013s | 3.273s | 5.286s |

| Sample | Pod | CPU | RAM |
|---|---|---:|---:|
| before | openrag-backend-65b8fd87ff-5jpkp | 4m | 820Mi |
| before | opensearch-0 | 135m | 5202Mi |
| after | openrag-backend-65b8fd87ff-5jpkp | 352m | 774Mi |
| after | opensearch-0 | 242m | 5204Mi |

Les échantillons CPU/RAM OpenSearch et backend avant/après sont conservés dans le JSON.

## I. Query decomposition signal

status: C. HIGH PRIORITY
reason: 63 composantes CORE restent absentes après la fermeture RRF à K=200; 63 avaient été retrouvées par au moins une probe non canonique.

## J. Qwen readiness

benchmark_ready_for_qwen: true
requirements_remaining: ["Qwen3-Embedding-0.6B representations and matching dense index"]

## K. Generality check

business/domain terms in benchmark code: 0

case-specific terms confined to benchmark definition: yes

## L. Validation

benchmark_tests: pass
Ruff: pass
Mypy: pass
git_diff_check: pass

production_modified: no
gitops_modified: no
commit: no
push: no
deploy: no

## M. Conclusion

DISCOVERY BENCHMARK BASELINE COMPLETE
