# ADR 0001 — Source provenance as a bounded W3C PROV-O profile

## Status

Accepted for implementation.

## Context

OpenRAG already stores `source_url`, which is an access locator for the
retained or remote original. It is not a stable identity and cannot represent
relationships such as an attachment belonging to an email, an email belonging
to a thread, or a file being contained in an archive.

The OpenArchiver connector already knows email `thread_id`, RFC 5322
`Message-ID`, and the many-to-many relation between emails and attachments.
The current ingestion contract transmits only the file and `source_url`, so
that structure is lost before indexing.

## Decision

OpenRAG implements a strict profile of the
[W3C PROV-O ontology](https://www.w3.org/TR/prov-o/):

- `source_url` remains the optional, mutable address used to inspect a source;
- `source_provenance.entity` identifies the current `prov:Entity`;
- `source_provenance.relations` stores directed, typed relationships;
- every OpenRAG role has exactly one full PROV-O predicate URI;
- arbitrary JSON-LD and arbitrary predicates are rejected at ingestion;
- relationship targets are source entities, not OpenSearch chunk IDs;
- inverse links are queried from directed relations and are not copied into a
  mutable `source_linked` field on parent documents.

The initial OpenRAG roles are:

| OpenRAG role | PROV-O predicate | Intended use |
| --- | --- | --- |
| `attachment_of` | `prov:wasMemberOf` | MIME attachment → email |
| `member_of` | `prov:wasMemberOf` | email → discussion thread |
| `reply_to` | `prov:wasInfluencedBy` | reply → referenced email |
| `references` | `prov:wasInfluencedBy` | email → RFC 5322 ancestry reference |
| `contained_in` | `prov:wasMemberOf` | archive entry → archive |
| `occurrence_of` | `prov:specializationOf` | mailbox copy → canonical message |
| `derived_from` | `prov:wasDerivedFrom` | transformed document → source entity |
| `primary_source` | `prov:hadPrimarySource` | secondary entity → primary evidence |

Each chunk repeats the canonical provenance envelope and safe flattened
keyword fields. Repetition is intentional: OpenSearch has no joins, and every
retrieved chunk must remain independently verifiable. The canonical envelope
retains relation pairing; flattened arrays exist only for filtering and
reverse traversal.

## Email thread rules

- Keep each email as an independently citable document.
- Namespace connector thread IDs by source; never treat an opaque `threadId`
  as globally unique.
- Preserve RFC 5322 `Message-ID` as a stable alternate identity. Preserve
  `In-Reply-To` as `reply_to` and ordered `References` as separate
  `references` relations; ancestry must not be mislabeled as a direct reply.
- Use normalized subject only as a fallback clue, never as proof of a thread.
- Preserve all email↔attachment relations. A single arbitrary parent must not
  replace an existing many-to-many relationship.
- A thread view is assembled at retrieval time and reports explicit coverage;
  it is not indexed as an LLM-generated source of truth.

## Security and integrity invariants

- Provenance never grants access. Every related document is independently
  filtered by the current OpenSearch/DLS identity.
- Identifiers are length-bounded and reject control characters.
- Relation count and alternate identifiers are bounded to protect token,
  request, and mapping size.
- Re-ingestion replaces the provenance for the new document generation; it
  must not leave stale inverse lists on other documents.
- Unknown fields, roles, predicates, and schema versions fail explicitly.

## Consequences

Legacy documents without `source_provenance` remain readable. New ingestion
clients may send the versioned provenance object. Search, chat, file listing,
and SDK responses expose it without changing the meaning of `source_url`.

The parent source-lifecycle branch owns this contract. Retrieval branches may
subsequently add thread expansion and exhaustive coverage on top of the same
stable entity and relation fields.
