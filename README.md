# Cloud in a Bottle

Your corner of the cloud.

Deploy, use, and share any app on a server you control. Built on the idea that modern software lives in the cloud, and if you want to be in control of your digital life, you need your own place in the cloud too.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

> **Early access.** Cloud in a Bottle is in active beta. We want you to try it and share feedback! Note that no password recovery exists yet and we don't hold keys to your instance so we can't reset it. Response time on issues and PRs may be slow as we're heads-down on the core product.



## Why Cloud in a Bottle

Most people have no access to the cloud that isn't mediated by a company with different incentives than theirs. Open source web software exists but running it somewhere means fighting infrastructure that most people don't want to touch.

Cloud in a Bottle is the project our team needed and couldn't find: a corner of the cloud that's genuinely yours. Where apps install as easily as on your phone, and the data lives on hardware you control.

## What people deploy

- Personal tools — AI-generated apps, scripts, and utilities with nowhere useful to host them
- Open source software — Matrix, Minecraft servers, notes apps, project management tools
- Dev and creative tools — coding agents, image-making software, anything you built and want to share with a real URL
- Anything Docker-compatible — add an `openhost.toml` manifest to any repo and it's deployable

## Managed hosting

If you'd rather not run your own server, [Cloud in a Bottle](https://openhost.imbue.com/plans) provisions one for you: your SSH key, your data, your instance. We just set you up with everything you need to get going, and then it's all yours from there.

---

## Quick start

### Local development

```bash
# run the router directly on your machine (needs rootless podman)
openhost up --dev

# optional: test app subdomains locally with lvh.me
# (for example: http://my-app.lvh.me:8080)
openhost up --dev --zone-domain lvh.me

# check prerequisites
openhost doctor

# stop everything
openhost down

# update code (git pull + dependency sync)
openhost update
```

### Server deployment

```bash
# full setup on a fresh Ubuntu 24.04 server
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e initial_user=root \
  --private-key=~/.ssh/YOUR_SSH_KEY

# fast re-deploy (sync code + restart service)
ansible-playbook ansible/deploy.yml -i <IP>, -e domain=<domain> \
  --private-key=~/.ssh/YOUR_SSH_KEY

# verify
curl https://<domain>/health
```

See `ansible/readme.md` for prerequisites and full details.

---

## How it works

The router is a Python app that provides a web dashboard for deploying and managing apps. It reads `openhost.toml` manifests from app repos, builds container images from each app's `Dockerfile`, runs each app in its own isolated container, and proxies inbound HTTP requests to the right app by subdomain.

### Server mode

When deployed to a server via Ansible, the router runs as a systemd service alongside CoreDNS and Caddy:

```
┌─────────────────────────────────────────────┐
│  Server (Ubuntu 24.04)                      │
│                                             │
│  CoreDNS (:53)  -- ACME DNS-01 challenges   │
│  Caddy (:443)   -- TLS termination          │
│  Router (:8080) -- app mgmt + reverse proxy │
│  rootless Podman -- app containers          │
└─────────────────────────────────────────────┘
```

CoreDNS serves DNS for the deployment domain, enabling automatic TLS certificate acquisition via ACME DNS-01. Caddy terminates TLS on `:443` and proxies to the router on `:8080`.

All persistent data (database, TLS certs, app data) lives under `data_root_dir` (default: `/opt/openhost`).

### Dev mode

`openhost up --dev` runs the router directly on your machine: HTTP only on port 8080, no TLS, no CoreDNS, no Caddy. Requires rootless podman for running app containers.

---

## Cloudflare tunnel setup (no port forwarding)

For public internet access without opening home router ports. See `docs/cloudflare-local-tls-plan.md` for the full runbook and `docs/cloudflare-local-laack-xyz-setup-log.md` for a real-world setup log.

---

## Local subdomain testing (optional)

To test host-based app routing locally, run dev mode with a local zone domain:

```bash
openhost up --dev --zone-domain lvh.me
```

Then open app URLs like:

- `http://my-app.lvh.me:8080`
- `http://another-app.lvh.me:8080`

Notes:

- Optional — intended for local development only.
- The default local flow is `http://localhost:8080`.
- Port 80 is not required. If you want no port in the URL, add your own local port-forwarding from `:80` to `:8080`.

---

## Operating modes

| Mode | Command | What It Does |
|------|---------|--------------|
| dev | `openhost up --dev` | Runs router directly on host, HTTP only (needs rootless podman) |
| server | `ansible-playbook ansible/setup.yml` | Deploy to any VPS or bare metal with automatic HTTPS (see `ansible/readme.md`) |

---

## Components

| Component | Path | What It Does | Status |
|-----------|------|--------------|--------|
| CLI | `routerd_cli/` | `openhost` command: up, down, doctor, update | Buggy — has stale VM flags, being reworked |
| compute_space CLI | `compute_space_cli/` | Compute space management CLI | Working |
| compute_space | `compute_space/compute_space/` | HTTP routing, app deployment, auth, reverse proxy | Working |
| dau_tracker | `apps/dau_tracker/` | DAU tracking + version check | Working |
| partaay | `apps/partaay/` | Distributed event hosting | Working |

---

## Development setup

```bash
# install pre-commit hooks (requires pre-commit: pip install pre-commit)
pre-commit install
```

### [Sculptor](https://github.com/imbue-ai/sculptor) workspace setup script

Add the following in Settings → Repositories → openhost → Workspace setup command. Sculptor is Imbue's coding environment — skip this if you use a different IDE.

```bash
cp -r [YOUR_LOCAL_OPENHOST_CHECKOUT]/.idea .
pre-commit install
```

Adapt to any IDE config or other git-ignored files you'd like copied over.

---

## Testing

```bash
# run all lightweight tests (from project root)
pixi run -e dev pytest

# run with podman integration tests enabled
pixi run -e dev pytest --run-containers
```

Server deployment prerequisites and test setup are documented in `ansible/readme.md`.

---

## Key docs

The user-facing manual lives in `docs/src/` and is served at `https://<zone>/docs/`. No build step — edit a markdown file and reload the page to see the change.

- `docs/src/introduction.md` — introduction + table of contents
- `docs/src/creating_an_app.md` — guide to building apps
- `docs/src/manifest_spec.md` — `openhost.toml` app manifest specification
- `docs/src/routing.md` — subdomain + path routing model
- `docs/src/data.md` — persistent + archive data tiers
- `docs/src/user_identity.md` — identity / login flow for apps
- `docs/src/oauth.md` — OAuth integration with external services
- `docs/src/cross_app_services.md` — app-to-app service calls

`docs/src/SUMMARY.md` is the sidebar table of contents — add new pages there to surface them in the manual.

---

## Agent skill

An agent skill that gives an AI coding agent context for deploying and debugging apps on Cloud in a Bottle via the `oh` CLI. Install with:

```bash
npx skills add cloud-in-a-bottle/cloud-in-a-bottle --skill openhost-context
```

The skill works best with the `cb` CLI installed and logged in:

```bash
uv tool install "cb @ git+https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
cb instance login
```

Once set up, ask your coding agent to package any existing project for Cloud in a Bottle and deploy it directly — no manual manifest editing required.

---

## License

Cloud in a Bottle is provided under the [AGPL-3.0 license](LICENSE).

We may move to a different license in the future — something like a [fair source license](https://fair.io/licenses/) — with the intent that personal use will always be unrestricted, while commercial use may be scoped to support a sustainable project.

---

## About Imbue

We build honest software. Often open source. Tools that help people think, create, and build. Tools that are loyal to you. 

- [Explore Imbue's code on GitHub](https://github.com/imbue-ai)
- [Check us out at imbue.com](https://imbue.com/)
- [Follow @Imbue_AI on X](https://x.com/imbue_ai)
