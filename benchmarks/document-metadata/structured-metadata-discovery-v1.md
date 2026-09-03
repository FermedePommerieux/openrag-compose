# Structured metadata discovery phase 1

The internal request contract is:

```text
openrag.structured-document-query v1
free_text: string
metadata_filter: openrag.metadata-filter v1 | null
```

Explicit constraints are never removed, weakened, or inferred from the free
text. There is no natural-language parser in phase 1.

Supported fields are production and modification day/month/year with explicit
source-local or UTC basis; MIME; format family; extension; explicit source
document type; source system/entity type/entity family/connector/parent
collection; exact normalized creator, last modifier, producer and creator
application observations; binary SHA-256; and conflict flags. Filename
basename remains internal and is rejected by the search allowlist. Archive and
ingestion calendar filters are not yet projected and therefore fail closed as
unsupported rather than being reinterpreted as production dates.

The query complexity contract is at most 32 predicates, nesting depth 4,
64 `IN` values, 16 OR branches, 512 side-index rows per page, 512 occurrence
ids per retrieval terms partition and 50,000 visible eligible occurrences.

The side-index query runs under the caller's DLS before any occurrence id is
returned. Retrieval then uses the ordinary user-scoped client independently.
Lexical and dense lanes receive identical occurrence-id partitions. Scores are
not changed; the top results from bounded partitions retain their original
lane scores before ordinary RRF. A no-filter request does not call the side
index.

Only `TRUE` rows are eligible. `UNKNOWN` and `FALSE` do not match positive
filters. Strong-Kleene negation preserves `UNKNOWN`. `EXISTS`/`NOT_EXISTS` are
separate operators; negated equality never includes missing metadata.

This module is not connected to the public API and no feature is enabled in
phase 1.
