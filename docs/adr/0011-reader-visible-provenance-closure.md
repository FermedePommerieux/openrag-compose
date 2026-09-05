# ADR 0011 — Reader-visible provenance closure

Status: accepted for the multi-user activation gate, 2026-09-05.

## Decision

A missing reader-visible representative is insufficient to distinguish a broken
provenance reference from a document hidden by DLS. Only a backend-internal
existence classifier may establish the latter. Its inputs are documentary
targets already asserted by validated visible provenance; it is not a public
lookup endpoint and cannot supply hidden documents to retrieval.

For each unresolved target, compare exact existence counts under the original
reader credentials and the index-control credentials. Query both primary and
alternate source identities, without chunk-zero or caller search filters. Use
`size: 0`, `_source: false`, complete shard/response validation, batches of at
most 100 and an absolute limit of 1,000 targets. Repeat both observations and
reject observed drift or inconsistent counts.

A stable zero reader count with a positive control count establishes exclusion
from this reader's closure. Remove that target from public relation accounting;
no hidden target ID, edge, document, content or count enters the certificate.
Zero in both views remains unresolved. A reader-visible target omitted by a
search filter or a broken representative remains unresolved. Classification
failure remains incomplete, with `provenance_visibility_unverified`.

All existing representative, pagination, execution, document digest and
certificate transport checks remain authoritative. Grouping-only email-thread
identities retain the existing typed reverse-query rule. This decision changes
neither ownership nor the occurrence/generation identity model.

## Validation and deployment boundary

Negative regressions retain failures for missing targets, incomplete execution,
unstable counts, malformed representatives and omitted visible documents. A live
isolated canary with two API-created local accounts validates reader-complete
cross-owner PROV-O closure, direct reads, citations, counts, metadata, Agent and
streaming against actual OpenSearch DLS and Langflow.

The backend change belongs to the auth candidate. It is not deployed by the
flow-only repair and does not authorize general multi-user activation.
