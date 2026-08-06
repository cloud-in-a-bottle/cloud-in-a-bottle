# The OpenHost platform service

> Apps reach platform operations over the **same** v2 service interface they use
> to call other apps, served in-process by the router (like the `installer`
> service) and gated by ordinary `permissions_v2` grants tied to the calling
> app's identity.

## Why

An app (e.g. a coding agent / "mind") should be able to do roughly what the
`oh` CLI does — deploy apps, manage them, read system info — without being
handed root-equivalent access. The unit of authority is a `permissions_v2`
grant tied to the calling app's identity, so the platform is just another
service in the same model apps already use for secrets, oauth, installer, etc.

## How an app consumes it

```toml
[[services.v2.consumes]]
service   = "github.com/imbue-openhost/openhost/services/platform"
shortname = "platform"
version   = ">=0.1.0"
grants    = [
  {capability = "deploy",       repo_url_prefix = "https://github.com/acme/"},
  {capability = "manage_apps",  target = "own"},
  {capability = "system_read"},
  {capability = "delegate_permissions"},
]
```

Then call `{OPENHOST_ROUTER_URL}/api/services/v2/call/platform/<endpoint>` with
`Authorization: Bearer $OPENHOST_APP_TOKEN`. The owner approves the declared
grants (same flow as any other service).

## Capabilities (grant payloads)

| Capability | Payload | Grants |
|---|---|---|
| `deploy` | `{capability:"deploy", repo_url_prefix:"..."}` | Deploy new apps whose repo_url starts with the prefix (`""`/`"*"` = any). The new app's `installed_by` is stamped with the caller. |
| `manage_apps` | `{capability:"manage_apps", target:"own"\|"all"\|"<app_id>"}` | View status/logs, stop/start, remove. `own` = apps this caller deployed; `all` = every app; `<app_id>` = one app. Does **not** include granting permissions. |
| `system_read` | `{capability:"system_read"}` | Read-only system info: version (git branch/SHA), storage snapshot, external listening ports, and a tail of the platform log. |
| `delegate_permissions` | `{capability:"delegate_permissions"}` | Grant an app **the caller deployed** any permission the **caller already holds** — never more (non-escalating). |

## Endpoints

```
POST /deploy                {repo_url, app_name?}            cap: deploy
GET  /apps                                                    cap: manage_apps
GET  /apps/<app_id>/status                                    cap: manage_apps (scoped)
GET  /apps/<app_id>/logs                                      cap: manage_apps (scoped)
POST /apps/<app_id>/stop                                      cap: manage_apps (scoped)
POST /apps/<app_id>/start                                     cap: manage_apps (scoped)
POST /apps/<app_id>/remove  {keep_data?}                      cap: manage_apps (scoped)
GET  /system                                                  cap: system_read
POST /delegate              {app_id, service, grant}         cap: delegate_permissions
```

## The propagating (non-escalating) delegation

This is the headline capability. An app A that holds `deploy` +
`delegate_permissions` + some grant `G` for service `S` can:

1. deploy app B (B's `installed_by = A`), then
2. `POST /delegate {app_id: B, service: S, grant: G}`.

The router grants `G` on `S` to B **only if**:

- A holds `delegate_permissions`, **and**
- A itself already holds exactly `G` for `S` (compared by canonical JSON, so
  key order doesn't matter), **and**
- B was deployed by A (`installed_by == A`).

So A can hand out copies of (a subset of) its own privileges but can never mint
a grant it doesn't have — no privilege escalation.

## Not included yet (tracked as follow-ups)

- SSH access to the compute space.
- Write access to platform settings.
- Managing API keys.
- Owner-facing UI for platform grants beyond the existing `permissions_v2`
  approval page.

## Relationship to the router-internal service pattern

This reuses the `installer` service's mechanism verbatim: a reserved
`*_SERVICE_URL` constant, interception in `_service_call_common`, in-process
dispatch in `service_call` with the router's DB, and `permissions_v2`-based
gating. See `core/platform_service.py` and `web/routes/platform_dispatch.py`.
