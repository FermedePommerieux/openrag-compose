# GT2 Consolidation + Completeness Control + Freeze + Cross-Domain Horizon Benchmark

Date d'audit : 2026-09-01
Topic : `orange-fibre-cross-domain-v1`

## A. Git baseline

```text
worktree:
/Users/eloiprimaux/Developer/openrag-compose-planner-calibration

working_branch:
agent/planner-calibration

baseline_HEAD_before_this_phase:
5e78422110e3 — benchmarks: record closure identity hashes

target_remote:
origin/pommerieux/v0.6.0-retrieval-v2-prov-o @ 4d45bba8

baseline_relation:
working branch ahead by 6 commits before this phase
```

`git fetch`, `git status`, `git branch -avv`, `git rev-parse HEAD` et
`git log --oneline --decorate -15` ont été exécutés. Le worktree et la branche
existants ont été conservés ; aucun commit expérimental antérieur n'a été réécrit.

## B. Corpus verification

Le corpus a été réénuméré avant toute décision de benchmark :

```text
visible_occurrences:          47,454
enumerated_occurrences:       47,454
distinct_document_ids:        47,400
distinct_source_entity_ids:   47,451
complete:                     true
occurrence_identity_sha256:   038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
```

Le digest est identique à la baseline. `comparable=true` pour le corpus ; cette
comparabilité ne lève pas le gate GT2.

## C. GT1 frozen baseline

GT1 `Surface pastorale` n'a pas été modifié.

| Niveau | CORE | CONTEXTUAL | NOT_RELEVANT |
|---|---:|---:|---:|
| Composants | 114 | 28 | 5 |
| Documents | 192 | 59 | 11 |

Provenance humaine canonique conservée :
`surface-pastorale-v1-ground-truth-human-v1.yaml`, SHA-256
`6a759d49141ceaaa27679bf9b39f90b2e0d5f889bcd5dd469501ca822fd19903`.

## D. GT2 topic/guideline

```text
topic_version:
orange-fibre-cross-domain-v1

guideline_version:
orange-fibre-cross-domain-guideline-v1

CORE:
Orange/Sosh AND fibre/Internet fixe AND centralité thématique

CONTEXTUAL:
information Orange + fibre/Internet fixe utile mais secondaire

NOT_RELEVANT:
mobile/4G/5G/téléphonie/ADSL/DSL seuls, autre opérateur sans rôle structurel
d'Orange, mention accidentelle ou absence d'apport documentaire réel
```

Les tests humains de substitution marque et service sont conservés. Ils ne sont
pas transformés en règles automatiques. La précédence est : jugement humain
document > jugement humain composant > suggestion automatisée. Les colonnes IA
des classeurs sources n'ont jamais servi de qrels.

## E. Human review consolidation

```text
component judgments:            350
document judgments stage 1:     249
document judgments stage 2:      88
total unique judged documents:  337
duplicate candidate_id:           0
conflicts:                        0
empty labels:                     0
invalid labels:                   0
```

Le digest canonique des 337 lignes consolidées est
`6bd581dca4b6d2ead6c64b6e2e04389d194877e7339be5cfd6bba703324e3cf6`.
Il s'agit d'un digest de consolidation, **pas** d'un ground-truth digest figé.

Les trois workbooks bruts sont versionnés sans modification :

| Source humaine | Rôle | SHA-256 |
|---|---|---|
| `orange-fibre-GT2-revue-humaine-pretrie-corrigee.xlsx` | composants | `53b49df89b3fe0324bd5f59d8cc56c71740e7cb664715aeb9ff6a4f96d98d637` |
| `orange-fibre-GT2-revue-a-faire.xlsx` | documents stage 1 | `ab5b671891dff8932a5e200b2f87f9258698778e87a6c20e51dcc996f48692fa` |
| `orange-fibre-GT2-derniere-revue-ciblee.xlsx` | documents stage 2 | `483b09f2ccf61a86591a75abe1c449e1cf6b3b486e24b2e7a19e3086868dc810` |

Les champs de provenance disponibles (`case_id`, `candidate_id`, `component_id`,
`document_id`, `occurrence_id`, `source_entity_id`, label, source, stage, feuille,
ligne, topic et guideline) sont conservés. Aucun `review_timestamp` n'existait
dans les sources ; il n'a pas été inventé.

## F. GT2 label distribution

```text
CORE:          48
CONTEXTUAL:    56
NOT_RELEVANT: 233
TOTAL:        337
```

Mapping numérique explicite : `CORE=2`, `CONTEXTUAL=1`, `NOT_RELEVANT=0`.

## G. Completeness control

Le contrôle a reconstruit le pool compact complet issu des lanes lexicales,
denses, RRF et de récupération metadata/relation. Les signaux n'ont servi qu'à
sélectionner des documents pour revue humaine.

```text
candidate universe:                         3,012 documents / 1,338 components
unjudged candidate universe:                2,675 documents
same thread/component as human CORE:            0
same document_id as human CORE:                 0
direct brand + fixed term in full preview:      0
exact non-degenerate CORE title families:       9
near non-degenerate CORE title families:        4
high-priority candidates found:                13
human review needed:                           13
automatic labels created:                       0

negative-control size:                         60
distinct negative-control components:          60
CORE found:                                     0
CONTEXTUAL found:                               0
NOT_RELEVANT found:                            60
estimated residual miss signal:                0.0%
```

La sélection des 13 lignes est déterministe : titres exacts ou proches
(`SequenceMatcher >= 0.82`) ancrés uniquement sur des titres document jugés
humainement `CORE`, avec exclusion des familles dégénérées `mail--<id>.eml`.
Elle comprend 9 familles exactes et 4 proches. Les signaux mobile/ADSL/téléphonie
et contexte secondaire sont affichés comme risques, jamais comme labels.

Les domaines expéditeur partagés (378 candidats) et longs identifiants numériques
partagés (77) ont été conservés comme diagnostics trop larges pour constituer à
eux seuls une priorité humaine. Le contrôle négatif a été reproduit exactement,
ordre inclus, par tri `SHA-256(candidate_id)` et sélection d'un seul document par
composant après exclusion des tranches prioritaires. Son taux nul n'est pas une
preuve mathématique d'exhaustivité.

## H. GT2 freeze

```text
GT2_FREEZE:
BLOCKED

blocker:
13 high-priority human document judgments pending

ground_truth_digest:
not available — GT2 is not frozen

corpus_digest:
038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7

guideline_version:
orange-fibre-cross-domain-guideline-v1
```

Le fichier `benchmarks/discovery/ground_truth/orange-fibre-cross-domain-v1.json`
n'a pas été créé. C'est l'application directe du contrat fail-closed.

## I. GT1 horizon results

Aucun horizon n'a été relancé dans cette phase, car le protocole autorise la
comparaison GT1/GT2 seulement après le freeze GT2. Référence historique gelée,
non réinterprétée comme une nouvelle mesure :

| Horizon | Seed doc recall | Seed comp recall | Post doc recall | Post comp recall | STRICT precision | Expansion | Coverage | Wall latency |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| 50/50 | 26.56% | 35.96% | 45.83% | 44.74% | 76.12% | 2.060 | complete | 5.49 s |
| 100/100 | 26.56% | 36.84% | 46.88% | 46.49% | 75.00% | 2.059 | complete | 3.81 s |
| 200/200 | 28.65% | 40.35% | 50.52% | 50.00% | 75.34% | 2.014 | complete | 3.64 s |

L'ordre de latence historique reste observationnel.

## J. GT2 horizon results

```text
50/50:   NOT RUN — GT2 freeze blocked
100/100: NOT RUN — GT2 freeze blocked
200/200: NOT RUN — GT2 freeze blocked
```

Aucun résultat de retrieval GT2 non figé n'entre dans la décision.

## K. Standard IR metrics

`nDCG@10`, `nDCG@100`, `MAP`, `Recall@100`, `Recall@200` et `Precision@100`
n'ont pas été calculés sur GT2 : les qrels restent un draft incomplet. Le tooling
équivalent déterministe est testé, mais son exécution sur GT2 est interdite par le
gate.

## L. Documentary metrics

Les métriques `Seed Document Recall`, `Seed Component Recall`, `Post-PROV-O
Document Recall`, `Post-PROV-O Component Recall`, précisions `STRICT`/`BROAD`,
facteur d'expansion, coverage et latence n'ont pas été mesurées sur GT2. Les
valeurs GT1 du tableau I ne sont que la référence historique.

## M. Determinism

```text
human consolidation order-independent:           PASS
duplicate/conflict/empty-label fail-closed:       PASS
title-family candidate selection deterministic:  PASS
negative control set reproducibility:             PASS
negative control order reproducibility:           PASS
review workbook imported formula error scan:      PASS
13 review labels initially blank:                 PASS

3 repeated horizon runs per GT/horizon:
NOT RUN — GT2 freeze blocked
```

Le protocole s'est arrêté avant le benchmark ; il n'existe donc aucune prétention
nouvelle de stabilité seed/scope/metrics pour cette phase.

## N. Cross-domain comparison

Impossible : GT1 est figé, mais GT2 ne l'est pas. Comparer des horizons contre les
337 qrels partiels laisserait le retrieval s'auto-évaluer sur un pool incomplet.

## O. Candidate horizon recommendation

```text
INSUFFICIENT CROSS-DOMAIN EVIDENCE
```

La production reste à `q1 50/50`, seed budget 100, RRF k 60, `multi_query=false`.

## P. Product change recommendation

```text
NO BUILD
NO DEPLOY
NO GITOPS CHANGE
NO PRODUCT DEFAULT CHANGE
```

La décision scope reste `TARGET/PROBE/HARD PROMISING BUT INSUFFICIENT EVIDENCE`.
Cette phase n'apporte pas de résultat GT2 figé permettant de rouvrir cette
architecture. `max_depth`, `max_entities`, `max_documents` et `batch` n'ont pas
été modifiés.

## Q. Tests

```text
full unit suite: 1,596 PASS
targeted benchmark unit tests: 56 PASS
artifact digest and gate regression: PASS
spreadsheet import, formulas, 13 blank labels: PASS
Ruff: PASS
```

La suite couvre consolidation, doublons/conflits, labels vides/invalides,
métadonnées guideline, digests d'artefacts, mapping qrel, génération des candidats,
reproductibilité du contrôle négatif, métriques IR, gate fail-closed et invariants
des artefacts versionnés. Les tests horizons/déterminisme existants restent
inchangés ; les captures nouvelles sont volontairement absentes.

## R. Remaining audit

```text
AUD-008:             not addressed in this session
AUD-009:             not addressed in this session
live cross-user DLS: not addressed in this session
```

Aucun de ces chantiers n'était un blocker direct de la consolidation humaine.

## S. Qwen

```text
QWEN_READINESS:
BLOCKED
```

Qwen n'a pas été modifié ni exécuté.

## T. Conclusion

```text
GT2 FREEZE BLOCKED
```
