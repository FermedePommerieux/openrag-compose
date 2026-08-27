# ADR 0008: Relevance-bounded archive audits

Status: accepted, amended by ADR 0009

## Context

The first archive-audit implementation deepened every vector lane from
`k=100` to as many as 10,000 neighbours. A nearest-neighbour engine always
returns a tail even when the tail is unrelated: similarity is not a calibrated
probability. Sending that tail through model-based classification, two
independent readers, coordinators and validators made one audit economically
unusable without proving better recall.

The contextual reviewer initially labelled documents but retained them for full
reading, then a cost optimization allowed a grounded `irrelevant` label to
exclude them. That optimization reduced work but made an excerpt-level model
decision an invisible recall gate; ADR 0009 removes it.

## Production calibration

On 2026-08-27 the production `documents` index contained 75,393 chunks. The
pastoral/sylvopastoral DDT query matched 26 distinct documents through the
independently exhausted lexical predicate. Because the OpenAI account had no
remaining credit, the K curve used the stored large embedding of a lexical
match as a no-cost relevant-anchor probe. It measures neighbourhood growth,
not end-to-end recall:

| K | distinct vector documents | overlap with lexical set | semantic-only |
|---:|---:|---:|---:|
| 25 | 6 | 6 | 0 |
| 50 | 7 | 6 | 1 |
| 100 | 17 | 9 | 8 |
| 250 | 101 | 12 | 89 |
| 500 | 250 | 21 | 229 |
| 1,000 | 573 | 23 | 550 |

K=100 is the smallest tested neighbourhood that materially complements
lexical retrieval. The growth after 100 is predominantly unsupported noise.

## Decision

- Exhaust lexical predicates and the selected high-signal PROV-O relations.
- Bound every semantic lane to K=100 and disclose the unsearched semantic tail.
- Calibrate vector scores against independently lexical-supported documents;
  an uncalibrated vector lane remains excluded.
- Keep the plausible union bounded at discovery, then retain every admitted
  document for full reading. ADR 0009 removes the excerpt-level LLM exclusion
  that this ADR originally allowed.
- Allow `OPENRAG_AUDIT_REASONING_MODEL` to separate high-volume audit workers
  from the final chat model. Production uses GPT-5.6 Luna for workers and keeps
  GPT-5.6 Sol for the final answer.
- Never report semantic completeness. Report lexical exhaustion, provenance
  fixpoint, reviewed candidate counts, exclusions and the bounded K policy.

## Consequences

The system now seeks complete coverage of plausible evidence rather than
literal traversal of weak vector neighbours. This is a deliberate precision,
cost and truth trade-off: a relevant item outside lexical, PROV-O and the first
100 semantic neighbours can be missed, and the certificate must say so.

Luna is 20 times cheaper on uncached input and roughly 16.7 times cheaper on
output than Sol at the public rates verified on 2026-08-27. That ratio is only
a cost projection. Model quality must be evaluated on fixed, labelled archive
questions once provider credit is available; no lower model is promoted solely
because it is cheaper.
