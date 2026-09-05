# ADR 0010 — Local credentials on the existing OpenRAG principal

Status: inventory completed before implementation, 2026-09-05. Activation is gated
by the separate validation report; this ADR does not claim production activation.

## Baseline and ancestry

After `git fetch --all --prune`, canonical
`origin/pommerieux/v0.6.0-retrieval-v2-prov-o` is
`6e8b8a095928739a9acade2ca017a6a405edb19e`. None of `69f215d0`, `14dd04cd`,
or `c985086e` is its ancestor. The smallest deployed correctness base is
`69f215d0bc56c0a9cd83d0f8ee5505780d91a9f9` (canonical plus `5147fc3e` and
`69f215d0` P0 coverage repairs). Production backend image is retrieval-v2.109,
frontend .105, Langflow .71; GitOps origin/main is `7d481d718825c3a707afbde613e7bed4e8f381bc`.
The live preflight returned no-auth, SQLite application DB, OpenSearch green,
RuntimeBehavior MATCH, locked managed flow v18, backend/frontend 1/1 and Langflow 2/2.
The original checkout has unrelated edits and is untouched. New worktree:
`openrag-compose-multiuser-local-auth`, branch `agent/multiuser-local-auth`.
Occurrence/generation candidates and migration tooling are excluded.

## Existing inventory

| Concern | Existing authority and path |
|---|---|
| Application user | `db/models/user.py`: `users.id`, immutable PK; unique `(oauth_provider, oauth_subject)`; encrypted PII; `is_active` exists but is not checked by old browser sessions |
| Persistence | `db/engine.py`: `DATABASE_URL`, defaults to durable `data/openrag.db`; production uses this SQLite DB, not Langflow PostgreSQL |
| Roles | `roles`, `user_roles`, `permissions`, `role_permissions`; `services/rbac_service.py`; RBAC opt-in, no first-user admin bootstrap |
| Workspace | one deployment workspace; `workspace_config` stores configuration, no user tenant/membership table; roles associate users with this workspace |
| User creation | `services/user_service.ensure_user_row`: provider/subject upsert, same subject usually preserved as PK, UUID on collisions; legacy-only email merge; unrelated providers with same email stay distinct |
| Request principal | `session_manager.User` currently distinguishes provider `user_id` from `db_user_id`; `auth/request_identity.py` attaches DB ID, then downstream ownership often uses `user_id` |
| Browser sessions | `SessionManager` signs seven-day JWTs with existing RSA/JWKS machinery; `auth_token` HttpOnly/Lax cookie; in-memory user registry; logout previously only clears cookie |
| Local passwords | absent in OpenRAG; no hash, login, reset, create-user API; permission catalog already contains users:list/read/invite and roles:assign |
| Langflow | installed 0.11.2 has native UUID/username/password/is_active/is_superuser accounts and authentication service; these own flow-engine access, separate from OpenRAG users and DLS; backend uses technical API key and forwards end-user identity independently |
| Google | `AuthService`, Google Drive OAuth verification, app_auth callback and session issuance; currently the only browser app OAuth login |
| Microsoft | MSAL, OneDrive/SharePoint OAuth adapters and connector principals; data-source OAuth, no existing Microsoft app-login button/route |
| OIDC/upstream | IBM AMS cookie/header/gateway JWT and configurable issuer verification; OpenRAG itself serves OIDC discovery/JWKS for OpenSearch; there is no generic browser OIDC provider list |
| Frontend | `contexts/auth-context.tsx`, `/login`: Google or upstream IBM; `/api` Next proxy strips prefix; `/users/me` reports provider ID and DB roles |
| Anonymous | missing Google credentials and IBM disabled implicitly enables `AnonymousUser`; default anonymous RBAC role admin; this is development compatibility, unsuitable as auth initialization fallback |
| Ownership | documents/metadata/knowledge_filters use owner, allowed_users, allowed_principals; chat ownership uses application DB; connectors retain existing provider alias mappings |
| DLS | securityconfig/roles.yml: JWT sub → `${user.name}`; owner/allowed_users, legacy verified email aliases, connector lookup row; ownerless records deliberately shared; no OpenSearch admin role for normal users |
| Product propagation | browser cookie/Bearer → request identity → user-scoped OpenSearch; chat forwards original JWT through Langflow → backend retrieval/metadata/read/citation; `auth_context` uses contextvars |
| Counts | ASTRA-020: KnowledgeFilterService reads existence aggregation with admin client, exposing a global active_source_count for a shared filter |

## Ownership diagram and decision

```mermaid
flowchart TD
  L[Local login and Argon2id credential] --> U[Existing OpenRAG users.id]
  G[Google verified identity] --> U
  E[Existing upstream identity adapter] --> U
  U --> R[Existing workspace user_roles and permissions]
  U --> S[Existing JWT signer plus durable session record]
  S --> B[Backend request principal]
  B --> O[User-scoped OpenSearch DLS]
  B --> F[Langflow technical engine API]
  F --> T[Retrieval / metadata / read / citation with original user JWT]
  T --> O
  O --> C[Accessible-graph closure certificate]
```

OpenRAG support is PARTIAL: reuse its user DB, role catalog, signer, cookie and
request dependencies. Add a local credential as a method referencing `users.id`,
and revocable SQL session records. Do not federate Langflow technical accounts
or introduce another credential database. Local login identifiers are separate
from user IDs and emails; local IDs are random UUIDs. No public registration or
email-based linking; no local password is automatically attached to an external
account. Existing external IDs and historical ownership remain unchanged.

Deployment mode is explicit (`auto` for legacy compatibility, `local`,
`local_plus_external`, `external`, `no_auth`). Explicit authenticated modes fail
closed; local modes require RBAC and exclude the incompatible IBM gateway mode.
Optional Google login in mixed mode and Microsoft connector capabilities remain.
The first local administrator is created by an explicit operator CLI using a
password prompt; thereafter actual authenticated admin APIs manage local users.

## GenerationHead handoff (design only)

Choose BACKEND_ONLY_CONTROL_INDEX. User-readable `documents*` currently makes
ownerless records shared; GenerationHead is control state, not documentary
evidence. Store it in a distinct `openrag_generation_control_v1` index outside
all user index patterns. Only the backend index-admin client may query/CAS it.
Reads first resolve an occurrence through the user-scoped document client, then
the backend may resolve that occurrence's current generation, then reads all
evidence again through the same reader-scoped client. Never return control rows,
hidden occurrence IDs, or global generation counts. Metadata and provenance
retain the occurrence ACL and reader-relative coverage contract.

Before resuming identity v1, test two same-owner occurrences with independent
heads and a second principal: deny GET/search/mget/alias access to the control
index for both ordinary users; prove lifecycle race/CAS behavior, separate
owner boundaries and no hidden control identifiers in responses. The architectural
choice addresses the ownerless control-record flaw, but implementation/live
canary and all original migration gates are still required. No migration is run here.

## Controlled validation outcome — 2026-09-05

Local credentials, durable principals, account lifecycle, provider-independent
startup and browser login through Next are validated. The real OpenSearch/
Langflow canary uses two API-created local readers and temporary application
storage. Both readers pass lexical/dense/hybrid isolation, metadata predicates/
pagination/counts, shared-filter reader counts (ASTRA-020), direct reads,
citations, ordinary leaf coverage and retrieval streaming. Each user-scoped
client receives 403 querying the separate backend-only control index.

Full multi-user activation remains **PARTIAL** with two observed gates:

1. A valid typed `email_message → reply_to → email_message` relation toward
   the other reader's hidden document exposes neither target nor edge, but the
   existing P0 validator records `provenance_target_unresolved` and refuses
   complete coverage (`graph_traversal_failed`, `profile_invalid`). The same
   readers obtain complete certificates after removal of the canary cross-edge.
   This branch deliberately preserves that deployed P0 rule. Resolving the
   conflict with a complete reader-accessible closure requires an explicit
   provenance/ACL contract; suppressing the failure would weaken P0, and querying
   the hidden target globally would violate the current traversal contract.
2. The existing managed Agent flow can retrieve, read and cite with the local
   principal, but a request requiring metadata produces `METADATA_TOOL_REQUIRED`
   without a metadata tool/callback. This occurs for both readers. A direct
   metadata-filtered backend search succeeds. No managed flow, model or prompt
   has been changed to conceal this missing path.

Production remains on its original images and anonymous mode. RuntimeBehavior
is MATCH; identity v1 remains blocked. External-provider coexistence is code/
configuration validated; no live Google, Microsoft or OIDC login was performed.
