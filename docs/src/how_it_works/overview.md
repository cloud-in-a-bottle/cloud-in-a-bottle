# Overview

An instance is one machine running one service: the router (`openhost.service`). The router faces the network, and everything else on the machine is either a process it supervises or an app container it started.

## The pieces

- **Router**: serves the dashboard and the API, proxies every request to the right app, builds and runs app containers, and manages domains and certificates. Listens on `:8080`.
- **Caddy**: terminates TLS on `:443` and forwards to the router; redirects `:80` to HTTPS. Skipped on HTTP-only installs.
- **CoreDNS**: authoritative DNS for your zone, so certificate challenges can be answered and misc other DNS entries created (eg for SMTP/email). Runs only when the zone is delegated to the machine.
- **App containers**: one rootless Podman container per app, each in its own user namespace, with only the data directories it asked for mounted in. Nothing reaches an app except through the router.
- **System agent**: a small root helper for the things the unprivileged `host` user cannot do itself, such as host configuration and software updates.

Caddy and CoreDNS are child processes of the router, so their output appears in the router's logs and they stop when it stops.

## A request, end to end

A browser opening `https://notes.mycooldomain.com/`:

1. **DNS**: the wildcard record `*.mycooldomain.com` resolves to the machine's public IP.
2. **TLS**: Caddy terminates the connection on `:443` with the wildcard certificate and forwards to the router over loopback.
3. **Match**: the router reads the `Host` header, finds which of its domains owns it, and takes `notes` as the app name.
4. **Auth**: unless the app declared that path public, the request must carry a valid owner session; otherwise the browser is sent to the login page.
5. **Proxy**: the request is passed to the app's container port, with identity headers set by the router. WebSockets are proxied the same way.

A request to the domain itself, with no app subdomain, is the dashboard.

## Access

By default every path of every app requires the owner to be logged in. The router enforces this before it proxies anything, so an app never sees an unauthenticated request unless it asked for one.

There are three ways a request can authenticate:

| Credential | Used by | How |
|---|---|---|
| Session cookie | Browsers | `session_token`, set at login. Opaque and stored in the router's database, so it can be revoked. Valid four weeks, and scoped to the domain, so one login covers the dashboard and every app on it. |
| API token | The `bottle` CLI, scripts, agents | `Authorization: Bearer <token>`. Created and revoked in the dashboard or with `bottle tokens`. |
| App token | App containers | `OPENHOST_APP_TOKEN`, injected into each container and used to authenticate [cross-app service calls](../creating_an_app/cross_app_services.md). |

Apps can open up specific routes by listing them in `public_paths` in their manifest. Those routes are proxied without authentication, and it is then the app's job to decide who may do what. To help, the router sets `X-OpenHost-Is-Owner: true` on requests that do carry a valid owner session, so an app can serve a public page and still show the owner an edit button. Any `X-OpenHost-*` header supplied by the client is stripped before the app sees it.

## On the machine

The instance keeps everything under one data directory, `data_root_dir` in `config.toml`, which is `~/.openhost/local_compute_space/` for the user the service runs as (`/home/host/...` on a provisioned server).

| Path | Contents |
|------|----------|
| `/home/host/openhost` | The checkout the service runs from |
| `<data dir>/config.toml` | Instance configuration |
| `<data dir>/persistent_data/` | App permanent data, plus `openhost/`: the router database, TLS certificates and keys, the generated Caddy and CoreDNS config |
| `<data dir>/temporary_data/` | App scratch space, build and container logs |
| `<data dir>/app_archive/` | The archive tier mount |

Read on for [Routing](./routing.md), [Data](./data.md), and [Logs](./logs.md).
