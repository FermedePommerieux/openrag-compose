# Coverage integrity authority

Status: accepted for the ASTRA-001, ASTRA-002, ASTRA-003 and ASTRA-011 repair.

The authority chain is:

```text
OpenSearch execution facts
  -> validate_search_response()
  -> validated provenance traversal and immutable document facts
  -> certify_scope_coverage()
  -> openrag.scope-coverage certificate
  -> verify_scope_coverage_certificate()
  -> Agent retrieval guard
```

`src/services/scope_coverage_contract.py` is the sole scope decision and
transport verification implementation. `retrieval_service.py` re-exports it
for existing callers. Langflow executes a verbatim generated copy of the
stdlib contract because its component runs outside the backend package.
`scripts/sync_scope_coverage_contract.py` synchronizes the component and flow;
byte-equality and differential tests prevent drift. A valid incomplete
certificate is transport-valid but cannot make the Agent terminal.

Every required lexical/dense request, metadata candidate search, graph
direction/page/observation and document page uses the same OpenSearch response
validator. Timeout must explicitly be false; shard totals must establish full
successful execution with no failed shards or failures. Skipped shards are
already included in successful shards. Optional `terminated_early` must be
false when returned. Exhaustive pages require exact totals and ascending
continuation keys. These rules follow the [OpenSearch Search API](https://docs.opensearch.org/latest/api-reference/search-apis/search/).
Missing execution evidence fails closed. Identical partial observations cannot
repair a failed execution. Verified hits may be retained with explicit failure
facts. Ranked/ANN membership and retrieval budgets are unchanged.

`validate_provenance_representative()` validates every seed and discovered
representative through `SourceProvenance`. The versioned envelope is semantic
authority. Present flattened values must match its indexed projection. Null
optional transport fields represent absent projections. Owner values, when
present, must have the indexed string shape; public/shared access is decided
by the existing DLS client, not by requiring the reader to equal the owner.
Invalid envelopes never become empty leaves. Diagnostics identify the visible
document and invalid field category without exposing hidden relation targets.
An asserted documentary target without a visible representative prevents
certification. An email-thread grouping identity is closed through the
existing typed reverse query, without asserting a separately readable document.

Direct reads pin document identity, ingestion generation, expected chunk count,
content digest, occurrence fields where present, owner and filename. These
facts originate in the versioned ingestion profile on the chunks, not in the
number of hits remaining in OpenSearch. Every page must match the pinned
profile, exact count, contiguous indices and ascending cursor. Chunk text is
hashed before its manifest is retained. The authenticated cursor carries the
pinned profile and already verified chunk manifest, bound to the existing
principal/filter fingerprint; it cannot skip chunks or substitute a generation.
Legacy cursors without these facts fail closed and require a fresh read.

`verify_complete_document()` is the shared final verifier for direct and scope
reads. It requires the expected count, unique ordered chunk identities,
contiguous indices and the canonical ingestion digest. Hash byte ordering is
implemented once in `document_manifest_sha256()`; raw text verification uses
`verified_chunk_manifest()`.

Managed flow migration v17 recognizes the exact settings-normalized v16 graph,
replaces the guard code, preserves workspace-owned prompt/model/provider
values, and uses the existing lock/verification lifecycle. Operator code
customizations remain protected. SQLite/WorkspaceConfigService retains
functional ownership; GitOps only selects deployment artifacts.

No retrieval default, limit, PROV-O policy rule, metadata planner, occurrence
identity design, OpenArchiver behavior or model selection changes are included.

Certificate transport preserves present empty lists and null facts, as well as canonical failure-code order. Differential regressions include the actual Retrieval component JSON projection and Agent state projection. Managed flow v18 migrates only exact known v16/v17 graphs while retaining WorkspaceConfig-owned prompt and model values.
