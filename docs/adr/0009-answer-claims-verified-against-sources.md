# ADR 0009: Verify answer claims against sources

Status: accepted

## Context

An archive audit previously asked a reasoning model to label each discovered
document from one excerpt and allowed a grounded `irrelevant` label to remove
that document before full reading. This reverses the burden of proof: an
excerpt cannot prove that the unread pages or later messages contain no
relevant evidence. It also spends model tokens on a destructive classification
whose false negatives are invisible to downstream verification.

The truth-first requirement is instead about the answer. Every factual claim
that reaches the user must be supported by the original pieces, and every
verified material finding must survive answer construction.

## Decision

- OpenSearch bounds the plausible candidate union through exhausted lexical
  lanes, calibrated semantic neighbourhoods and selected PROV-O relations.
- No LLM classification can remove a document from that admitted union. Every
  candidate is read in full under the existing snapshot and coverage checks.
- Redundant leaf readers construct candidate findings from bounded source
  batches. Unsupported or uncertain findings are withheld after two independent
  validators inspect their cited chunks.
- Loss-checked coordinators construct the final answer-claim plan without
  dropping any source-verified leaf finding.
- Two independent validators judge each final answer claim directly against
  only the original chunk segments it cites. Validation is partitioned by both
  claim count and serialized source size, so the archive is never resent as one
  oversized prompt.
- The synthesis exposes an `answer_contract`: the final chat model may state
  only verified findings and must represent every one, with exact `chunk_id`
  citations. Missing representation is an answer-coverage failure, not evidence
  that the source was irrelevant.
- Discovery metadata explicitly reports
  `pre_read_exclusion_applied=false`, zero excluded documents, and the
  `claims_against_cited_source_chunks` verification policy.

## Consequences

False-negative risk is controlled at the OpenSearch discovery boundary rather
than hidden in an excerpt-level LLM gate. Once a document enters the candidate
union, relevance noise can consume reading work but cannot erase evidence.

Final validation consumes tokens only for claims and their cited source pieces,
not for every retrieved candidate. The bounded OpenSearch K policy still means
whole-corpus semantic completeness is not certified; that limitation remains
visible in the audit response.
