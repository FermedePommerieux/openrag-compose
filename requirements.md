# Multi-user production activation requirements

Source of truth: the approved Pommerieux activation brief and the official
[OpenRAG installation options](https://docs.openr.ag/install-options/). This is
a self-managed deployment; runtime secrets and functional model/prompt settings
remain outside Git and GitOps.

## Baseline and scope

- REQ-001: Base `agent/multiuser-production-activation` on the exact validated
  local-auth candidate containing P0 coverage repair. Acceptance: ancestry is
  recorded and no Identity v1 occurrence/generation commit is included.
- REQ-002: Preserve existing user changes and historical worktrees. Acceptance:
  implementation occurs only in isolated activation worktrees.
- REQ-003: Keep the 652k-chunk Identity v1 migration and ASTRA-007 through
  ASTRA-019 remediation out of scope. Acceptance: no GenerationHead corpus
  creation, occurrence rewrite, or listed remediation is executed.

## Build and staged deployment

- REQ-004: Pass the required unit, auth/RBAC, frontend, browser, planner,
  flow-migration, metadata-Agent, DLS, provenance, archive, lint, type and lock
  gates before activation. Acceptance: each result is retained in evidence.
- REQ-005: Build only changed backend, frontend and, if required, Langflow
  artifacts. Acceptance: source SHA, immutable tag and digest are recorded.
- REQ-006: Roll out compatibility-capable artifacts before switching auth.
  Acceptance: the old production behavior remains healthy after the artifact
  rollout and before the auth-mode change.
- REQ-007: Define a forward-compatible rollback for artifacts, auth mode and
  additive database schema before switching auth. Acceptance: rollback cannot
  accidentally restore anonymous administrator access.

## Runtime and authentication

- REQ-008: Keep WorkspaceConfigService/SQLite and the managed Langflow flow as
  runtime authorities. Acceptance: `/api/settings/runtime-behavior` reports
  `MATCH` for models, prompt, retrieval settings and flow guards.
- REQ-009: Preserve an explicit planner model versus `Use chat model` fallback
  as distinct persisted states. Acceptance: fallback follows a later chat-model
  change without copying that model into the planner field.
- REQ-010: Activate the existing validated local authentication mode, with
  external adapters optional and preserved. Acceptance: local login works with
  providers absent or unavailable; anonymous protected access returns 401.
- REQ-011: Apply the additive Alembic migrations after a consistent database
  backup. Acceptance: schema revision is current and existing user IDs and
  external identities are unchanged.
- REQ-012: Bootstrap administrator `eloiprimaux` through the masked operator
  mechanism using the reserved immutable UUID and mandatory password change.
  Acceptance: the temporary credential cannot access the workspace, replacement
  succeeds, the old credential is rejected and the resulting session is admin.
- REQ-013: Validate local-user lifecycle and RBAC using controlled real users.
  Acceptance: create, disable, enable, reset, revocation and self-protection pass.

## DLS, provenance and managed flow

- REQ-014: Enforce symmetric two-user isolation across lexical, dense, hybrid,
  metadata, direct reads, citations, knowledge filters and Agent streaming.
  Acceptance: hidden rows affect neither results nor reader-visible counts.
- REQ-015: Treat DLS-hidden provenance targets as an undisclosed accessible-graph
  boundary, while genuinely invalid or missing asserted targets remain a P0
  failure. Acceptance: visible, hidden, invalid and mixed coverage cases pass,
  with no hidden IDs or counts exposed.
- REQ-016: Preserve ASTRA-001, ASTRA-002, ASTRA-003, ASTRA-011 and ASTRA-020.
  Acceptance: every named gate remains PASS.
- REQ-017: Keep the validated structured metadata tool in the locked managed
  flow while retaining runtime-owned values. Acceptance: deterministic metadata
  requests invoke the DLS-scoped tool and RuntimeBehavior remains MATCH.

## Archive ownership migration

- REQ-018: Re-inventory under a writer pause using an isolated, paged, streamed,
  bounded and checkpointed process. Acceptance: file, document and projection
  counts are current; UNKNOWN and CONFLICT are both zero; RSS/CPU/latency/bytes
  are recorded; any unexplained OOM stops the run.
- REQ-019: Execute a representative production canary, exact rollback and
  identical reapply before full migration. Acceptance: ownership, identity,
  content, metadata, chunks, embeddings, provenance, DLS and search all match.
- REQ-020: Migrate only reviewed ownership/account placement with CAS guards and
  per-item states. Acceptance: resume is idempotent and no content, source
  identity, metadata truth, chunking, embeddings or provenance is rewritten.
- REQ-021: Complete exact post-migration accounting and integrity verification.
  Acceptance: totals are unchanged, reviewed owner mapping is exact, and every
  failure or exception is individually reviewable.

## Production health and handoff

- REQ-022: Maintain 5/5 nodes, backend 1/1, Langflow 2/2, frontend 1/1, green
  OpenSearch and Ready Fleet throughout rollout. Acceptance: no unexplained
  CrashLoop, restart or OOM remains.
- REQ-023: Validate only the backend-only GenerationHead control-index boundary.
  Acceptance: user clients are denied, the backend internal client is allowed,
  and no corpus generation heads are created.
- REQ-024: Update current documentation only after successful activation.
  Acceptance: local multi-user is marked production, external providers optional,
  no-auth development-only, archives per-account, planner fallback documented,
  and Identity v1 is not described as active.
- REQ-025: Prepare the validated result for canonical integration without force
  push or automatic Identity v1 merge. Acceptance: exact fast-forward/merge
  requirements and final application/GitOps SHAs are reported.

