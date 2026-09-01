# GT2 Freeze + Cross-Domain Candidate-Horizon Benchmark

Date d'audit : 2026-09-01
Topic : `orange-fibre-cross-domain-v1`

## A. Git baseline

```text
worktree: /Users/eloiprimaux/Developer/openrag-compose-planner-calibration
branch: agent/planner-calibration
baseline HEAD: c151b8c4d0fdffa9ed5a462f77bc529d951343be
baseline commit: benchmarks: gate GT2 freeze on human completeness
upstream: origin/pommerieux/v0.6.0-retrieval-v2-prov-o
baseline relation: ahead by 7 commits
```

Le worktree, la branche et les artefacts antérieurs ont été conservés. Aucun
default produit n'a été modifié et aucun déploiement n'a été effectué.

## B. Corpus verification

Le corpus a été vérifié avant et après la campagne :

```text
visible_occurrences:          47,454 -> 47,454
enumerated_occurrences:       47,454 -> 47,454
distinct_document_ids:        47,400 -> 47,400
distinct_source_entity_ids:   47,451 -> 47,451
complete:                     true -> true
occurrence_identity_sha256:   038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
comparable:                   true
```

## C. Frozen ground truths

GT1 `surface-pastorale-v1` n'a pas été modifié.

| GT | Niveau | CORE | CONTEXTUAL | NOT_RELEVANT |
|---|---|---:|---:|---:|
| GT1 | documents | 192 | 59 | 11 |
| GT1 | composants | 114 | 28 | 5 |
| GT2 | documents humains | 48 | 57 | 245 |
| GT2 | composants métriques dérivés des qrels documents | 33 | 51 | 184 |
| GT2 | métadonnées composants revues | 58 | 21 | 271 |

Provenance GT1 canonique : `surface-pastorale-v1-ground-truth-human-v1.yaml`,
SHA-256 `6a759d49141ceaaa27679bf9b39f90b2e0d5f889bcd5dd469501ca822fd19903`.
Définition exécutée : SHA-256
`43963a033a203aa29f62d518e6b940749b4cba62ed1cb9c44586ddcac8c97341`.

## D. GT2 topic and guideline

```text
topic_version: orange-fibre-cross-domain-v1
guideline_version: orange-fibre-cross-domain-guideline-v1
CORE: Orange/Sosh + fibre/Internet fixe + centralité thématique
CONTEXTUAL: information Orange + fixe utile mais secondaire
NOT_RELEVANT: mobile/4G/5G/téléphonie/ADSL seuls, autre opérateur sans rôle
              structurel, mention accidentelle ou absence d'apport documentaire
mapping: CORE=2, CONTEXTUAL=1, NOT_RELEVANT=0
```

La précédence reste jugement humain document > jugement humain composant >
suggestion automatisée. Les colonnes IA n'ont jamais servi de qrels.

## E. Pass 3 import and consolidation

Le workbook fourni a été conservé byte-for-byte :
`orange-fibre-GT2-completeness-review-pass-3.xlsx`, SHA-256
`9745b82639775948aa0a4efcb3ae92f3338a244f1885b898bb1006180cb93fb5`.
Seuls `human_label` et `review_notes` ont été importés, liés aux 13
`candidate_id` attendus.

```text
pass 3 rows:          13
CONTEXTUAL:            1
NOT_RELEVANT:         12
duplicate identities: 0
conflicts:            0
empty labels:         0
invalid labels:       0
workbook values match: true
```

| Stage | Documents | CORE | CONTEXTUAL | NOT_RELEVANT |
|---|---:|---:|---:|---:|
| 1 | 249 | 47 | 56 | 146 |
| 2 | 88 | 1 | 0 | 87 |
| 3 | 13 | 0 | 1 | 12 |
| consolidé | 350 | 48 | 57 | 245 |

Le digest canonique des 350 qrels consolidés est
`0efd69628ca4e81a89aa0c4857ad3468db598038cda41237a9994817fa4a88c0`.
Les trois stages sont disjoints ; aucun document non jugé n'a été injecté.

## F. Completeness control rerun

Le contrôle complet a été rejoué sur le même univers non borné que lors de la
sélection pass 3.

```text
candidate universe:              3,012 documents / 1,338 components
human-judged documents:            350
unjudged documents:              2,662
same CORE/relevant component:        0
same CORE/relevant document_id:      0
direct Orange/Sosh + fixed:          0
CORE title family exact/near:        0
secondary Orange/Sosh + fixed:       0
new high-priority candidates:        0
human review needed:                 0
automatic labels created:            0
```

Les domaines expéditeur (377), identifiants numériques partagés (83/84),
signaux fixe sans marque (27), mobile-only (121) et ADSL/DSL-only (2) restent
des diagnostics larges ou des guards, jamais des labels. Le contrôle négatif
historique de 60 documents, tous jugés `NOT_RELEVANT`, reste valide. Les 2 662
documents non jugés restent `UNJUDGED`.

## G. Freeze gate and digest

```text
GT2_FREEZE: PASS
benchmark_authorized: true
blockers: []
pending_high_priority_candidates: 0
ground_truth_digest: a1f53f7f1e42969b287d4778f846fd7a3c86a0bee0d2c0d0cb46a1cc6e10ff6a
corpus_digest: 038987ca2eb70b2e56d674bc45e9b60fe00652e72296249dba0252d6964fafd7
unjudged_documents_defaulted_to_not_relevant: 0
```

Le freeze est reproductible : deux générations successives ont produit les
mêmes hashes source, qrels et ground truth.

## H. Benchmark protocol

La campagne a exécuté exactement 18 runs : 2 GT × 3 horizons × 3 répétitions.
La seule variable était le couple `lexical_candidates/dense_candidates` :
50/50, 100/100, 200/200. Tous les autres paramètres sont restés fixes : q1,
RRF k=60, seed budget=100, `multi_query=false`, scope `documentary-prov-o v1`,
`max_depth=8`, `max_entities=500`, `max_documents=250`, `batch_size=50`.

Contexte exécuté : application
`c151b8c4d0fdffa9ed5a462f77bc529d951343be`, identité produit no-auth,
workspace runtime par défaut, filtres knowledge `{}`. Les 18 runs ont terminé
sans erreur de transport.

## I. GT1 documentary results

Valeurs identiques sur les trois répétitions pour les métriques documentaires.
Latences en moyenne sur trois runs.

| Horizon | Seed doc | Seed comp | Post doc | Post comp | Precision STRICT | Expansion | Coverage | Graph | Read | Total |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| 50/50 | 26.56% | 35.09% | 45.83% | 43.86% | 76.12% | 2.060 | 100%, complete | 0.253s | 4.875s | 5.128s |
| 100/100 | 26.56% | 35.96% | 46.88% | 45.61% | 75.00% | 2.059 | 100%, complete | 0.267s | 3.842s | 4.109s |
| 200/200 | 28.65% | 39.47% | 50.52% | 49.12% | 75.34% | 2.014 | 100%, complete | 0.269s | 3.569s | 3.838s |

La vue BROAD post-PROV-O passe respectivement à 53.78/50.70%,
54.58/52.11% et 57.37/54.93% en recall document/composant ; sa précision
judged-only vaut 100% dans les trois cas.

## J. GT2 documentary results

| Horizon | Seed doc | Seed comp | Post doc | Post comp | Precision STRICT | Expansion | Coverage | Graph | Read | Total |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| 50/50 | 45.83% | 66.67% | 60.42% | 66.67% | 52.38% | 2.563 | 100%, complete | 0.359s | 3.357s | 3.717s |
| 100/100 | 45.83% | 66.67% | 60.42% | 66.67% | 48.89% | 3.165 | 0%, document limit | 0.484s | 3.752s | 4.237s |
| 200/200 | 45.83% | 66.67% | 60.42% | 66.67% | 45.83% | 3.086 | 0%, document limit | 0.393s | 3.961s | 4.354s |

À 50/50, la traversal épuise naturellement la frontier à 205 documents. À
100/100 et 200/200, les trois répétitions atteignent 250 documents avec une
frontier non vide : `coverage.complete=false`, `status_code=document_limit_reached`.
Les chiffres de qualité de ces deux horizons sont donc descriptifs et ne
peuvent pas justifier un changement.

La vue BROAD GT2 post-PROV-O reste à 29.52% document / 28.57% composant ; la
précision judged-only décroît de 57.14% à 53.33%, puis 50.00%.

## K. Condensed standard IR metrics

Les métriques standard sont calculées après condensation des seuls documents
jugés. Les non-jugés sont retirés du ranking, jamais transformés en zéros.

| GT | Horizon | nDCG@10 | nDCG@100 | MAP | Recall@100 | Recall@200 | Precision@100 |
|---|---:|---:|---:|---:|---:|---:|---:|
| GT1 | 50/50 | .7773 | .6194 | .2669 | .2669 | .2669 | 1.0000 |
| GT1 | 100/100 | .8039 | .6275 | .2709 | .2709 | .2709 | 1.0000 |
| GT1 | 200/200 | .8905 | .6636 | .2908 | .2908 | .2908 | 1.0000 |
| GT2 | 50/50 | .4441 | .4056 | .1235 | .2286 | .2286 | .5714 |
| GT2 | 100/100 | .5068 | .4033 | .1205 | .2286 | .2286 | .5333 |
| GT2 | 200/200 | .5704 | .4070–.4071 | .1214 | .2286 | .2286 | .5000 |

Pour GT2, 38, 34 et 33 seeds non jugés sont respectivement exclus. La variation
de nDCG@100 à 200/200 est la conséquence de la dérive d'ordre auditée ci-dessous.

## L. Coverage, resource, and latency audit

Les scopes GT1 sont naturellement complets. Seul GT2 50/50 est naturellement
complet ; GT2 100/100 et 200/200 restent fail-closed. Les chunks lus sont :
GT1 6 239 / 6 241 / 5 768 et GT2 3 656 / 4 734 / 4 725.

Le delta `ru_maxrss` est uniquement un proxy de processus : moyennes 23 760,
853 et 341 KiB pour GT1, puis 0, 1 707 et 1 024 KiB pour GT2. Il est dépendant
du high-water mark et ne permet pas une comparaison RAM robuste. La CPU n'était
pas observable séparément ; aucune conclusion ressource ne repose sur ces
valeurs.

## M. Determinism audit

```text
seed identity sets stable:        PASS (6/6 groups)
scope identity sets stable:       PASS (6/6 groups)
ordered scope stable:             PASS (6/6 groups)
ordered seed stable:              FAIL (4/6 groups pass)
documentary metrics stable:       PASS (6/6 groups)
standard metrics stable:          FAIL (5/6 groups pass)
overall determinism gate:         FAIL
quality interpretation authorized: false
```

La répétition 2 déplace un résultat aux rangs 89–90 pour GT1 50/50 et aux
rangs 38–40 pour GT2 200/200. Les répétitions 1 et 3 sont identiques. Les
ensembles seed et scope ne changent jamais.

RRF trie de façon déterministe par score puis identité persistante. Le score
réciproque change néanmoins (`.00917431 -> .00925926` sur GT1 et
`.01259644 -> .01274013` sur GT2), ce qui prouve une variation de rang dans une
lane avant fusion. L'observation est cohérente avec la lane dense OpenSearch k-NN
approximative ; la lane lexicale a un tie-break explicite. Aucun signal ne pointe
vers le planner ou la traversal scope. Ce diagnostic n'autorise pas un correctif
produit dans cette phase.

## N. Cross-domain comparison

Les horizons supérieurs augmentent certaines métriques GT1, mais ils n'améliorent
pas le recall GT2 et provoquent une closure incomplète à 100/100 et 200/200. Le
protocole impose en outre l'arrêt de l'interprétation qualité après échec du gate
de déterminisme. Il n'existe donc pas de preuve cross-domain suffisante pour
modifier l'horizon.

## O. Candidate horizon recommendation

```text
KEEP 50/50
```

C'est une décision de non-changement et de sécurité, pas une affirmation que
50/50 est qualitativement optimal.

## P. Product and scope-limit decision

```text
NO BUILD
NO DEPLOY
NO GITOPS CHANGE
NO PRODUCT DEFAULT CHANGE
```

`max_depth`, `max_entities`, `max_documents` et `batch_size` n'ont pas été
modifiés. Cette campagne isole l'horizon de retrieval ; elle ne réouvre pas le
choix adaptive soft/hard scope et n'autorise aucune nouvelle limite fixe.

## Q. Verification

```text
targeted benchmark tests: 14 PASS
full unit suite:           1,600 PASS
Ruff focused checks:       PASS
Mypy focused checks:       PASS
freeze generation twice:  byte-stable hashes PASS
campaign executions:      18 complete / 0 transport errors
```

La collecte repository-wide `uv run pytest -q` reste indisponible à cause d'une
collision de namespace `tests.test_retrieval_provenance`; `pytest tests/` requiert
en outre le package optionnel `openrag_sdk`. La suite officielle `tests/unit/`
est entièrement verte. Ces deux problèmes de collecte préexistent et ne sont
pas liés à cette phase.

## R. Remaining audit

```text
AUD-008:             not addressed in this session
AUD-009:             not addressed in this session
live cross-user DLS: not addressed in this session
dense k-NN repeat-order drift: audited, unresolved product-side
```

## S. Qwen

```text
QWEN_READINESS: BLOCKED
```

Qwen n'a pas été modifié, exécuté ou déployé.

## T. Conclusion

```text
GT2 FROZEN - CROSS-DOMAIN EVIDENCE INSUFFICIENT
```

Les qrels GT2 sont gelés et digestés. Le benchmark a été exécuté seulement après
ce freeze, mais les failures coverage et determinism empêchent toute promotion
d'un horizon supérieur.
