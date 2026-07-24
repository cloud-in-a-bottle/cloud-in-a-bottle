# CLI API Routes

This page documents every HTTP route the `oh` CLI (the `compute_space_cli`
package) calls on a running OpenHost zone. 

## Authentication (applies to every route)

- Every CLI request carries `Authorization: Bearer <api-token>`. Tokens are
generated in the settings page then set in the CLI, which can create more
tokens
- All requests are owner-only, except /health which is unused.

## Route summary

| Method | Path | `oh` command | Access |
|---|---|---|---|
| GET | `/` | `oh status` | read |
| GET | `/dashboard` | `oh instance login` (token verify) | read |
| GET | `/api/apps` | `oh app list` (+ name→id resolution) | read |
| GET | `/api/app_status/{app_id}` | `oh app status`, `--wait` polling | read |
| GET | `/api/app_diagnostics/{app_id}` | `oh app diagnostics` | read |
| GET | `/app_logs/{app_id}` | `oh app logs` | read |
| GET | `/api/compute_space_logs` | `oh logs` | read |
| GET | `/api/version` | `oh version` | read |
| GET | `/api/diagnostics` | `oh diagnostics` | read |
| GET | `/api/tokens` | `oh tokens list` | read |
| POST | `/api/add_app` | `oh app deploy` | **write** |
| POST | `/reload_app/{app_id}` | `oh app reload` | **write** |
| POST | `/stop_app/{app_id}` | `oh app stop` | **write** |
| POST | `/remove_app/{app_id}` | `oh app remove` | **write** |
| POST | `/rename_app/{app_id}` | `oh app rename` | **write** |
| POST | `/api/tokens`| `oh tokens create` | **write** (credential) |
| DELETE | `/api/tokens/{token_id}` | `oh tokens delete` | **write** (credential) |

`oh instance ssh`, `oh app ssh`, and `oh instance rsync` go through other commands
and are not listed. `oh curl` injects the bearer token and supports any `curl`.

Path parameters: `{app_id}` is an opaque string, typically gotten through /api/apps.
`{token_id}` is an integer.

### App lifecycle (mutating)

#### `POST /api/add_app` — `oh app deploy`
Clone a git repo, build the image, and start routing to it.
- **Request body:** `repo_url` (str, required), `app_name` (str, optional),
  `clone_dir` (str, optional), `port_overrides` (object `{label: int}`),
  `grant_permissions_v2` (bool), `permissions_v2_grants` (array).
- **Success (200):** `{ok: true, app_id, app_name, status: "building"}`.
- **Errors:** `401 {error, authorize_url}` when the repo needs GitHub auth (the
  CLI prints the URL to authorize); `400 {error}` for an invalid repo or
  manifest; `503 {error}` when the archive backend is unhealthy.

#### `POST /reload_app/{app_id}` — `oh app reload`
Rebuild and restart an app. With `--update`, pull latest git first.
- **Request body:** `update` (bool, default false), `approve_new_permissions`
  (bool, default false).
- **Success (200):** `{ok: true}`. May instead return
  `{ok, permissions_required: [...], error}` when an updated manifest declares
  new ungranted permissions and `approve_new_permissions` is false.
- **Note:** a separate `GET /reload_app/{app_id}` exists for OAuth re-entry; the
  CLI does not use it.

#### `POST /stop_app/{app_id}` — `oh app stop`
Stop a running app's container.
- **Request body:** none.
- **Success (200):** `{ok: true}`. **Errors:** `404`; `409 {error: "App is being removed"}`.

#### `POST /remove_app/{app_id}` — `oh app remove`
Remove an app; delete its data unless `--keep-data`. Teardown runs in the
background, so the CLI then polls `GET /api/app_status/{app_id}` until `404`.
- **Request body:** `keep_data` (bool, default false).
- **Success (202):** `{ok: true}`, or `{ok: true, already_removing: true}` if a
  removal is already in flight. **Errors:** `404`; `503` (unhealthy archive or
  worker-spawn failure).

#### `POST /rename_app/{app_id}` — `oh app rename`
Rename an app (changes its subdomain).
- **Request body:** `name` (str, required).
- **Success (200):** `{ok: true, name, app_id?}`. 
- **Errors:** `400` (empty / invalid name `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$` 
  / reserved conflict); `404`; `409` (being removed, or name already in use);
  `503` (unhealthy archive).

### App status, logs & listing (read-only)

#### `GET /api/apps` — `oh app list`
Also used to resolve an app **name → `app_id`** for almost every `oh app <name>`
subcommand.
- **Response (200):** array of `{app_id, name, status, error_message}`.

#### `GET /api/app_status/{app_id}` — `oh app status`
Also polled by deploy/reload `--wait` and by remove (waiting for `404`).
- **Response (200):** `{status, error, error_kind, git_branch, git_sha, git_dirty, container_id}`.
- **Errors:** `400 {error: "Invalid app_id"}`; `404 {error: "not found"}` (the
  sentinel the remove-poll waits for).

#### `GET /api/app_diagnostics/{app_id}` — `oh app diagnostics`
- **Query:** `download` (bool, default false; adds a download header).
- **Response (200):** a per-app diagnostics JSON bundle. **Error:** `404`.

#### `GET /app_logs/{app_id}` — `oh app logs`
- **Response (200):** **plain text** container logs (`--follow` re-polls).
  **Error:** `404`.

#### `GET /api/compute_space_logs` — `oh logs`
- **Response (200):** **plain text**, last 256 KiB of the zone/router log.
  **Error:** `503` (`"Log file not configured"`).

#### `GET /api/version` — `oh version`
- **Response (200):** `{branch: str|null, sha, short_sha, dirty}` (empty `sha`
  when not a git checkout).

#### `GET /api/diagnostics` — `oh diagnostics`
- **Query:** `download` (bool, default false).
- **Response (200):** a platform diagnostics JSON bundle (host/OS/Python
  versions, container runtime, disk usage, per-app summaries).

### API tokens

These mint and revoke the very credentials used to call this API, so a
permission matcher may want to gate them separately from ordinary writes.

#### `GET /api/tokens` — `oh tokens list`
- **Response (200):** array of `{id, name, expires_at: str|null, created_at, expired: bool}`.

#### `POST /api/tokens` — `oh tokens create`
- **Request body:** `name` (str), `expiry_hours` (str; a number or `"never"`).
- **Response (200):** `{token, name, expires_at: str|null}` — the raw token is
  returned **only here**. **Error:** `400 {error: "Expiry must be positive"}`.

#### `DELETE /api/tokens/{token_id}` — `oh tokens delete`
- **Response:** `204` no content (idempotent).

### Instance / auth verification

#### `GET /` — `oh status`
Connectivity + token check. Renders the dashboard for a valid token; the CLI
only inspects the status code.

#### `GET /dashboard` — `oh instance login`
Same handler as `/`. `oh instance login` treats `200` as a valid token and
anything else as failure.
