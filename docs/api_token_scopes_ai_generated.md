# Granular API token scopes — design (AI-generated draft)

> **Status:** design draft, pre-implementation. Captures decisions from the
> initial design discussion. This is an AI-generated doc — treat it as a
> record of intent, not as a hard spec. Human review pending.

## Problem

An OpenHost API token today is **root-equivalent**. Any valid, unexpired
bearer token passes the `require_owner_auth` guard and can reach *every*
owner route: deploy/remove apps, read app logs, mint more API tokens, change
the owner password, toggle SSH, restart the host, reconfigure S3 credentials.

`AuthenticatedAPIKey` is currently an empty marker class
(`core/auth/auth.py`) — a token carries no identity and no scope, so all
tokens are interchangeable and fully privileged.

We want to hand tokens to programs and agents without granting full access,
because some apps hold sensitive user data and agents are not a hardened
trust boundary. The fix: **granular, per-token scopes**, chosen at creation
time and editable later.

## Precedent in the codebase

We are copying the *shape* of the existing `permissions_v2` system (JSON
grants stored per row, a grant/revoke API, enforcement at a single router
chokepoint) — but tokens are a **separate concern** from `permissions_v2`.
`permissions_v2` governs **app-to-app service** access; token scopes govern
what a **bearer token** may do against the owner control plane. Different
table, different enforcement point, do not conflate them.

## Scope model (v1)

Scopes are `resource:action` strings stored as a JSON array on the token.
Twelve scopes, grouped into three tiers.

| Scope | Tier | Grants | Backing routes (today) |
|---|---|---|---|
| `apps:read` | read | List apps, status, diagnostics | `GET /api/apps`, `/api/app_status/*`, `/api/app_diagnostics/*` |
| `apps:logs` | read (⚠ user data) | Read app logs | `GET /app_logs/*` |
| `system:read` | read | Version, ports, storage/ssh status, compute_space logs, list domains | `/api/version`, `/api/listening-ports`, `/api/ssh-status`, `/api/storage-status`, `/api/compute_space_logs`, `/api/diagnostics`, `GET /api/domains` |
| `settings:read` | read | Read settings, owner username, remote | `/api/settings/get-remote`, `/api/settings/owner_username` |
| `apps:manage` | write | Deploy, clone, reload, stop, rename, set-remote | `/api/add_app`, `/api/clone_and_get_app_info`, `/reload_app/*`, `/stop_app/*`, `/rename_app/*`, `/set_app_remote/*`, `/api/check_port` |
| `apps:delete` | write (destructive) | Remove apps | `POST /remove_app/*` |
| `system:admin` | **owner-equiv** | Toggle SSH, restart router, drop docker cache, storage-guard, add/remove domains | `/toggle-ssh`, `/restart_router`, `/api/drop-docker-cache`, `/api/storage-guard`, `POST`/`DELETE /api/domains` |
| `settings:write` | **owner-equiv** | Update settings, **change password**, restart compute_space | `/api/settings/update`, `/api/settings/change_password`, `/api/settings/set-remote`, `/api/settings/restart_compute_space`, `/api/settings/owner_username` (POST) |
| `storage:admin` | **owner-equiv** | Configure archive backend / S3 creds | `/api/storage/archive_backend/*` |
| `tokens:manage` | **owner-equiv** | Create/list/delete API tokens | `/api/tokens*` |
| `permissions:manage` | **owner-equiv** | Grant/revoke app service permissions | `/api/permissions/v2/*`, `/api/services/v2/defaults*` |
| `identity:approve` | **owner-equiv** | Sign federated identity tokens as owner | `/identity/approve` |

### Design decisions baked into the table

1. **`apps:manage` is deliberately coarse.** We do *not* split
   start/stop/rename/deploy. Rationale: a token that can spin up an app
   should be able to fix its own mistake (stop + retry) rather than leave a
   broken app running; `start` without `stop` is meaningless. `apps:delete`
   *is* separate, because destruction deserves its own opt-in.

   Caveat: `rename` + deploy together can simulate a MITM-by-replace
   (rename the real app aside, deploy an impostor on its subdomain). This is
   the main reason `apps:manage` is "powerful" and is the strongest argument
   for the **ownership scope** in v2 (below). Until then, `apps:manage`
   should be documented as a broad grant.

2. **`apps:logs` is split from the other reads.** Logs are where user PII
   leaks. Keeping it distinct lets a token get broad read visibility
   (`apps:read` + `system:read`) *without* log access — the most important
   split for the agent-safety use case.

3. **The "owner-equivalent" tier.** These scopes let the holder escalate its
   own privilege or exfiltrate everything:
   - `tokens:manage` — can mint a fresh full-access token.
   - `permissions:manage` — can grant *any* consumer app *any* grant on
     *any* service; since services are how apps expose secrets (DB URLs,
     OAuth tokens), this is effectively "hand out all secrets."
   - `settings:write` — can change the owner password.
   - `system:admin`, `storage:admin`, `identity:approve` — host control,
     credential config, and signing as the owner respectively.

   **Policy:** these are all *allowed* as scopes (so an automation can be
   given exactly one), but each is documented as escalation-capable and
   ≈ owner. The token-creation UI must **warn** when any owner-equiv scope
   is selected.

### The `owner` super-scope

A token may hold the special scope `owner`, which means "all scopes." This:
- lets a human make a full-access token in one click, and
- is the **migration default** for existing tokens (see storage), so the
  change is non-breaking.

`owner` on a token is *not* the same as session-owner auth. See enforcement.

## Storage

Minimal change, matching repo conventions (raw SQLite, hand-rolled versioned
migration, hashed tokens, JSON-in-a-TEXT-column as already used by
`apps.public_paths` and `permissions_v2.grant_payload`):

```sql
-- migration v0014_api_token_scopes.sql
-- scopes is a JSON array of scope strings, e.g. '["apps:read","apps:logs"]'.
--
-- NOTE: the implemented migration does NOT use a DB-level DEFAULT.  A default
-- would be a silent privilege hole (an INSERT that forgot to set scopes would
-- mint a full-access token), so the column is `scopes TEXT NOT NULL` with no
-- default and every write path sets scopes explicitly.  Since SQLite can't add
-- a NOT-NULL-no-default column to a table with existing rows, the migration
-- rebuilds the table (create new shape, INSERT ... SELECT stamping '["owner"]'
-- and a backfilled token_id on existing rows, DROP old, RENAME) so pre-scopes
-- tokens keep full access via an explicit '["owner"]' on each row.
```

We deliberately do **not** add a child `api_token_scopes` table for v1. A
join table only earns its place when we add per-app scoping — which we are
explicitly deferring (see v2). A JSON column is enough for resource-type
granularity and mirrors existing precedent.

## Enforcement

Three small code touch points, all funneling through the single existing
auth chokepoint (`authenticate()` → owner guard in `web/auth/auth.py`):

1. **Accessor** (`core/auth/auth.py`): `AuthenticatedAPIKey` gains
   `scopes: frozenset[str]`. `validate_api_token()` reads the `scopes`
   column and populates it.

2. **Guard factory** (`web/auth/auth.py`): a `require_scope("apps:manage")`
   factory replaces bare `require_owner_auth` on each route. It passes if:
   - the accessor is a session **owner** (`AuthenticatedUser`, Origin-checked
     as today) — sessions stay fully unscoped; scopes only ever *restrict
     tokens*, never the human owner; **or**
   - the accessor is an `AuthenticatedAPIKey` whose `scopes` contains the
     required scope **or** the `owner` super-scope.

3. **Creation/update API** (`web/routes/api/system.py`): `POST /api/tokens`
   accepts a `scopes` list, validated against the known set; `PATCH
   /api/tokens/{id}` rewrites it. `GET /api/tokens` returns each token's
   scopes so the UI can show/edit them.

### Security invariant: scopes come only from the validated token

There is **no "I am owner" header** a caller can set. Owner identity is
always derived from either a session cookie (Origin-restricted, unforgeable
by app JS) or a bearer token that must sha256-match a real `api_tokens` row.
Scopes are read *only* from that validated row, never from client input. So
for any request the **router mediates**, an app cannot spoof owner or upgrade
its own scopes by setting a header — it would have to actually possess a
higher-scoped token. Keep it this way: never trust a client-asserted
identity/scope.

### Out of scope for this work — but must be tracked

The permission model only governs requests that **flow through the router**.
If apps can reach each other (or internal router endpoints) directly over the
network, bypassing the router, then per-token scopes are
necessary-but-not-sufficient, and each app's own auth becomes the only
barrier. That is a **network-isolation** concern, not something the token
scope schema can fix. Track it as a separate ticket; note it here so the
scope work isn't mistaken for full app-to-app isolation.

## Deferred to v2: ownership scope

We are **not** building per-app scoping in v1. Two flavors were considered:

- **Enumerated ACL** ("this token may touch apps X, Y, Z") — rejected: the
  grant set and UI grow with the number of deployed apps; unwieldy.
- **Ownership scope** ("this token may touch apps *it* deployed") — deferred
  but likely desired: one implicit rule, zero per-app config, and it directly
  answers "let an agent manage only its own app."

The data hook already exists: `apps.installed_by` (migration v0006) already
records which consumer initiated an install, and the installer v2 service
already uses it to scope status/logs queries per caller
(`web/routes/services_v2.py`). To keep the v2 door open cheaply, v1 should
ensure token-initiated deploys **stamp `installed_by`** with the token's
identity, so no backfill is needed when ownership scope lands. When it does,
`apps:manage`/`apps:delete`/`apps:logs` gain an "(own)" variant that adds an
`installed_by == this_token` check — no new grant rows, no per-app UI.

## Open questions (still under discussion)

1. **Default scope on creation** — back-compat leans `["owner"]`, but we may
   prefer forcing an explicit least-privilege choice for *new* tokens while
   the migration default stays `["owner"]` only for *existing* rows.
2. **Granularity** — is ~12 scopes the right altitude, or do we want coarser
   buckets (`read`/`write`/`admin`)?
3. **Token identity** — do scoped tokens need a stable id/name surfaced as
   `installed_by`, and what string do we stamp (token id? name?).
