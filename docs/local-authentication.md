# Local authentication candidate and operator runbook

This branch implements local authentication. It is **not activated in Pommerieux
production**. Activation requires the two-user product gates in the validation
report, including coverage and streaming. The default remains legacy `auto`.
The [architecture inventory and decision](adr/0010-local-authentication.md)
records the deployed P0 baseline and the separate identity-migration dependency.

## Deployment configuration

| `OPENRAG_AUTH_MODE` | Behavior |
|---|---|
| `local` | Local login; Google/Microsoft/OIDC are not required for authentication; anonymous requests are rejected |
| `local_plus_external` | Local login plus configured Google app login; local login works if Google is absent/unavailable |
| `external` | Existing Google or IBM gateway authentication; fails startup without a configured adapter |
| `no_auth` | Explicit legacy anonymous development behavior |
| `auto` (default) | Existing Google/IBM/implicit anonymous selection, unless local login was chosen during first-run onboarding |

Local modes require `OPENRAG_RBAC_ENFORCE=true`, `IBM_AUTH_ENABLED=false`, SQL echo
off, and the development role toggle off. Invalid authenticated configuration
fails startup; it never falls back to anonymous access. Use `local` for the
proposed Pommerieux authenticated deployment after all gates pass.

In `auto`, choosing a local administrator during first-run onboarding durably
enables local login **and RBAC**, even when the legacy RBAC environment default
is false. Explicit deployment modes retain priority over this choice.

`OPENRAG_AUTH_COOKIE_SECURE=true` is the default. Set it to `false` only for an
isolated HTTP development deployment; production must use HTTPS. The frontend
uses `/api/auth/me` to discover enabled login methods and proxies local account
operations to the backend. The proxy validates the browser Origin against its
public Host before translating it to the backend Origin; the backend validates
that origin again. A trusted ingress should also rate-limit login attempts.

Google app credentials retain their existing variables. Microsoft Graph/MSAL
remains the existing OneDrive/SharePoint connector authentication, not a new
Microsoft application-login button. The existing IBM/OIDC gateway adapter is
preserved in external mode; mixing that gateway's OpenSearch credential model
with local sessions is explicitly unsupported. No generic browser OIDC adapter
or automatic account linking is introduced.

Compose forwards the mode, secure-cookie and RBAC flags. Kubernetes/GitOps may
set these deployment flags and reference provider secrets using its existing
backend environment mechanism. GitOps must contain no local passwords/hashes
and must not take ownership of Agent/planner models, prompts or retrieval settings.

## Durable authority and principal

The existing application `DATABASE_URL` owns users, roles and workspace state.
Alembic `0008_local_auth` adds `local_credentials` and `auth_sessions` there.
Pommerieux's authority remains its persistent application SQLite database;
Langflow's PostgreSQL accounts remain technical flow-engine accounts.

Local usernames are case-insensitive ASCII identifiers, 3–64 characters, starting
with a letter/digit and otherwise allowing letters, digits, dots, `_` and `-`.
They resolve to immutable random `users.id` UUIDs. Passwords are 12–1024
characters, hashed with Argon2id (64 MiB, three iterations, parallelism four,
random library-generated salt). No password or hash is returned by account APIs.
Local accounts have no unverified email alias that could grant document access.

Local and newly issued Google sessions use the persisted internal ID in JWT
`sub`, request ownership and DLS. Existing external rows/IDs are preserved;
subject collisions resolve through the stored provider/subject mapping. Existing
legacy upstream subject/email compatibility remains documented in the ADR.
An external account never receives a local password automatically. Identical
email addresses do not link local and external accounts.

There is one deployment workspace (`default`), with existing role membership.
No parallel tenant or role system is added. Normal readers have no OpenSearch
administrator role. Documents, metadata, graph traversal and citations use the
same reader-scoped client. Shared filters do not grant document visibility;
`active_source_count` uses the reader client and returns unknown on count failure.

## Bootstrap and recovery

On a fresh installation, the browser opens `/onboarding/account` before the
existing model/provider assistant. The user can create a local administrator
with a username and confirmed password, or continue without a local account.
The form explains that creating the account makes sign-in mandatory. Creation
signs the new administrator in immediately; subsequent visitors see the login
page. Configured Google login remains available in automatic mode.

The choice is committed in the existing application database alongside the
first administrator and its session, using a one-time `migration_status` marker
(`local_auth_browser_setup_v1`). The backend loads it before initializing auth
services, so a restart retains both mandatory login and RBAC. A database error
or invalid saved choice fails startup. Password validation failure rolls back
the whole enrollment, and concurrent claims cannot create two first admins.

The browser offer requires a fresh workspace: no real identity, local credential,
prior bootstrap/choice, or started/edited onboarding. Existing installations
are marked closed at startup so resetting the provider assistant cannot reopen
enrollment. Choosing to skip also closes the offer permanently. Use operator
bootstrap below to enable local accounts later. Explicit `local` and
`local_plus_external` allow initial administrator creation without anonymous
skip; explicit `no_auth`, `external`, IBM gateway, development role-toggle and
SQL-echo configurations do not offer browser enrollment.

Use the same working directory, environment, persistent database and signing/
encryption keys as the backend. Back up the database before a version upgrade.
For a checkout, run from its root (in a container use its installed Python):

```sh
OPENRAG_AUTH_MODE=local OPENRAG_RBAC_ENFORCE=true PYTHONPATH=src \
  uv run python -m auth.local_admin bootstrap operator
```

The command prompts twice using `getpass`; do not pass a password in argv or
environment variables. It runs the additive application migration and creates
one durable administrator. A unique transactional marker prevents repeated
bootstrap, including concurrent attempts. No permanent bootstrap login exists.

For operator password recovery, using the same environment:

```sh
PYTHONPATH=src uv run python -m auth.local_admin reset-password operator
```

Recovery resets the existing account's password and revokes its sessions. It
does not change its immutable ID, role, or enabled state. An authorized active
administrator can re-enable another account through the API. An administrator
cannot disable their own current account through that API.

Administrators can open **Settings → Local users** to list accounts and their
immutable IDs, workspace roles and enabled state; create a user with an existing
workspace role; enable/disable accounts; and reset passwords. Disabling a user
or resetting a password revokes that user's sessions. The screen prevents
self-disable, and resetting one's own password signs the administrator out.
The navigation, server-rendered page and backend enforce administration access.

## Product API

The browser uses the `/api` prefix; direct backend routes omit it. Administration
requires both existing `users:invite` and `roles:assign` permissions, even when
the legacy RBAC bypass is set. Public enrollment is limited to the one-time
fresh-installation administrator choice; ordinary users cannot self-register.

| Method and backend route | JSON request / behavior |
|---|---|
| GET `/auth/me` | Enabled login methods and `local_setup_available` / `local_setup_can_skip` flags |
| POST `/auth/local/setup` | Fresh workspace only: `login`, `password`; atomically creates administrator/session and commits mandatory local login |
| POST `/auth/local/setup/skip` | Fresh automatic-mode workspace only: permanently skip local enrollment, retaining existing auth selection |
| POST `/auth/local/login` | `login`, `password`; creates HttpOnly, SameSite=Lax session cookie |
| GET `/users/me` | Internal `user_id`, authenticated state, provider, roles and workspace |
| POST `/auth/logout` | Revokes durable session and clears cookie |
| POST `/auth/local/password` | `current_password`, `password`; revokes all current account sessions |
| GET `/users/local` | Admin list with limit/offset pagination and `available_roles` from the existing role catalog; no hashes |
| POST `/users/local` | Admin create: `login`, `password`, optional existing `role` (default `user`) |
| PATCH `/users/local/{user_id}` | Admin `enabled`: true/false; invalidates existing sessions |
| POST `/users/local/{user_id}/password` | Admin reset: `password`; invalidates target sessions |

Sessions expire after eight hours and survive backend restart using SQL records
and the existing signing keys. Every authenticated backend hop checks expiry,
enabled state and credential version. Login attempts are bounded per process
per account/client and globally. Browser cross-site credential mutations are
rejected. API keys keep their existing independent lifecycle; in local mode they
check the live account and delegate a five-minute revocable session for tool
callbacks. Password reset does not revoke independent API keys. Account disable
blocks both keys and sessions. OpenSearch remains internal: it validates signed
JWT expiry/DLS, while immediate session revocation is enforced at the backend.

## Upgrade, rollback and staged verification

1. Validate the candidate and back up the current application database.
2. Deploy compatible backend/frontend with the existing auth mode retained.
3. Create controlled accounts in the intended persistent store using the
   operator/admin mechanisms; enable `local` only after all production gates pass.
4. Exercise both real readers through search, metadata, graph, reads, citations,
   shared-filter counts, Agent and streaming, and verify RuntimeBehavior MATCH.

Do not switch a production authenticated deployment to no-auth as recovery.
If auth has been activated, prefer a forward repair or put access behind a
maintenance boundary before rolling back to a version without local support.
The additive tables can remain during code rollback. An explicit Alembic downgrade
to `0007_add_knowledge_delete_anonymous` deletes only local credentials/sessions,
making those methods unusable, and preserves users/roles/external identities.
Back up first; re-upgrade restores schema, not deleted credentials.

`scripts/validate_local_multiuser.py` is an operator-only controlled live harness.
It clones only workspace configuration into a new temporary user database,
uses existing keys, creates uniquely named canary indices and a dedicated
Langflow key, starts a temporary callback listener, then removes those resources.
It must be authorized for its destination before execution. Evidence contains
canary IDs/results, never passwords. Remove its temporary DB/config/key artifacts
after collecting evidence. It never runs the production occurrence migration.

## Identity migration handoff

GenerationHead belongs in `openrag_generation_control_v1`, outside every user
index pattern. Only the backend control client resolves heads after a successful
reader-scoped occurrence authorization. Evidence is still fetched through the
reader client. The migration remains blocked until its dedicated implementation
and same-owner/two-occurrence/CAS/control-index-denial canaries pass. This auth
candidate neither activates identity v1 nor changes OpenArchiver.
