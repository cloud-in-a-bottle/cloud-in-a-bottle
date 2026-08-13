---
name: openhost-context
description: Deploy and debug apps on Cloud in a Bottle, a platform for self-hosting apps. Use when working with the `oh` CLI, deploying or reloading an app on a Cloud in a Bottle zone, or building a Cloud in a Bottle app.
---

# Cloud in a Bottle

Cloud in a Bottle is a platform for self-hosting apps. Each app runs in its own
container; the Cloud in a Bottle router terminates TLS, handles auth, and
proxies requests to apps by subdomain (`https://{app_name}.{zone_domain}/`).

You interact with a zone through the `oh` CLI, which injects auth for you.
Prefer `oh` over raw HTTP requests so you never have to handle tokens by hand.

## First time: install and log in

If `oh` isn't installed:

```bash
uv tool install "oh @ git+https://github.com/imbue-openhost/openhost.git#subdirectory=compute_space_cli"
```

Then the **user** logs in (this is interactive — ask them to run it):

```bash
oh instance login
```

A zone you've logged into is called an **instance**. List configured
instances and the URL each is reachable at (if none are listed, the user needs to do `oh instance login`)

```bash
oh instance list
```

## Targeting an instance

Most users have one instance; some have several. **Ask the user which
instance to use, and only touch that one, if multiple are available** — don't deploy to, reload, or
otherwise modify instances you weren't asked to.

Pass `--instance <name>` to target a specific one (works on any subcommand):

```bash
oh status --instance my-zone
oh app list --instance my-zone
```

If a single default instance is configured, `--instance` can be omitted.

## Safety

Zones serve the public internet. Be careful with anything that could open
unauthenticated access:

- By default every route requires the zone owner to be logged in. Routes
  listed in `public_paths` in `openhost.toml` are reachable by **anyone** —
  only expose paths that are meant to be public.
- Don't put API tokens in code, commits, or anything that might be pushed.
  `oh` already injects auth; reach for a raw token only if truly necessary
  (`oh instance token --instance <name>`), and keep it out of the repo.

## Deploy / update workflow

Deploy an app from a git repo URL (public, or private with auth):

```bash
oh app deploy https://github.com/you/my-app --name my-app --wait --instance my-zone
```

You can deploy from a branch with `https://github.com/you/my-app@branch_name`.

The router reads `openhost.toml` from the repo, builds the image from the
app's `Dockerfile`, and starts routing to it.

The common iterate loop — commit + push your changes, then pull & rebuild
on the zone:

```bash
git commit -am "..." && git push
oh app reload my-app --update --wait --instance my-zone   # --update = git pull first
oh app logs my-app --instance my-zone                     # check the result
```

Other app commands: `oh app status|list|stop|rename|remove`. Run
`oh --help` (or `oh app --help`) for the current, complete list.

## Debugging

- **Logs**: `oh app logs my-app --follow --instance my-zone` (app logs);
  `oh logs --instance my-zone` (zone/router logs).
- **Shell on the zone**: `oh instance ssh --instance my-zone`.
- **Authenticated HTTP**: `oh curl https://my-app.<zone-domain>/some/path`
  — runs `curl` with the user's API token token injected, so it behaves like an
  owner-logged-in request.
- **Test a page in a browser** the way a logged-in owner sees it: drive it
  with Playwright and inject the API token as an `Authorization: Bearer
  <token>` header. This matches the behavior of a request carrying the
  owner's login cookies. Get a token with `oh instance token` (handle it
  carefully — see Safety).

## Building a new app

Start from the template and build on top of it:

```
github.com/imbue-openhost/app-template
```

An app is any OCI container reachable over HTTP. It needs an `openhost.toml`
manifest and a `Dockerfile` at the repo root; it should listen on
`0.0.0.0:<port>` matching `runtime.container.port` in the manifest.

For the full app-authoring reference (manifest fields, the runtime
contract, injected env vars, data storage, auth, cross-app services):

- Read the manual on your own zone at `https://<zone-domain>/docs/` — it
  always matches the Cloud in a Bottle version you're running.
- Or read the source docs at
  `github.com/imbue-openhost/openhost/tree/main/docs/src` (start with
  `creating_an_app.md` and `manifest_spec.md`).
- You can also always clone `github.com/imbue-openhost/openhost

To reference Cloud in a Bottle's code and docs directly, you can always just clone the openhost repo locally:
```bash
git clone https://github.com/imbue-openhost/openhost.git
```
