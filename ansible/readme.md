
## prerequisites

- ansible installed locally: `uv tool install ansible-core` or however you like.
- a fresh ubuntu 24.04 server with root SSH access
- DNS records pointing your domain (and `*.domain`) to the server IP
    - NS record host.example.com -> ns1.host.example.com
    - A record ns1.host.example.com -> machine IP
- ACME account key at `ansible/secrets/certbot_private_key.json` (not in git; CI pulls from `ACME_ACCOUNT_KEY` GitHub secret — for local deploys, retrieve from there or your secret store). not needed in http-only mode (see below).

## runtime

app containers run under rootless podman as the `host` user, sharing podman's default single-user namespace (the one `/etc/subuid` allocates at user-creation time).  every bind mount uses idmapped mounts so container-root writes land on disk owned by the `host` user rather than the mapped subuid.  the server must be running on a filesystem that supports idmapped mounts (ext4, xfs, btrfs, tmpfs).  the bootstrap fails early with a clear error if this is not the case.

## full setup (fresh server)

```bash
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e initial_user=root --private-key=~/.ssh/YOUR_SSH_KEY
```

add `-e openhost_branch=<branch>` to deploy a branch, or `-e openhost_commit=$(git rev-parse HEAD)` to pin an exact commit. otherwise it defaults to remote's `main`. the ref must be pushed to github first.

the trailing comma after the IP is required (tells ansible it's a host list, not a file).

## fast re-deploy

pulls fresh code (default to origin's main), updates config, restarts the service:

```bash
ansible-playbook ansible/deploy.yml -i <IP>, -e domain=<domain> --private-key=~/.ssh/YOUR_SSH_KEY
```

## variables

pass with `-e key=value`.

| variable | default | purpose |
|---|---|---|
| `domain` | *(required)* | zone domain |
| `initial_user` | `root` | SSH user for the first play (creates the `host` user) |
| `public_ip` | target IP | public IP written into config / DNS |
| `openhost_branch` | `main` | git branch to deploy |
| `openhost_commit` | `main` | exact commit SHA, overridden by openhost_branch |
| `openhost_refspec` | *(none)* | extra ref to fetch, e.g. `refs/pull/N/merge` |
| `local_http_only` | `false` | localhost mode: no TLS / CoreDNS / Caddy |
| `bind_host` | `127.0.0.1` | router bind address |
| `claim_token` | random (printed) | claim_token for `/setup` |
| `acme_email` | `openhost@<domain>` | ACME account email (TLS mode) |
| `acme_directory_url` | LE production | override, e.g. staging or a local pebble |
| `skip_apt_upgrade` | `false` | skip `apt upgrade` in the packages step |
| `skip_service_start` | `false` | don't start the service at the end |
| `overwrite_existing` | `false` | overwrite existing config/secrets instead of preserving them (see [re-deploying](#re-deploying-an-existing-instance)) |

version precedence: `openhost_branch` > `openhost_commit` > default `main`.

> **openhost version compatibility.** These playbooks seed the domain + claim token via
> `first_boot.toml` and no longer write `zone_domain`/`tls_enabled` into `config.toml`. The deployed
> openhost code must be new enough to read `first_boot.toml` — i.e. the config/domains-consolidation
> change (the `samuel/local-support` branch, and `main` once it merges) or later. Pinning an
> `openhost_commit`/`openhost_branch` from **before** that change will boot with **no domain seeded**;
> deploy a matching or newer ref.

### cert_api broker (opt-in, alternative to the bring-your-own ACME key)

| variable | purpose |
|---|---|
| `cert_provider` | set to `cert_api` to fetch certs via the openhost-cert-api broker |
| `cert_api_base_url` | override the broker URL (optional) |
| `cert_api_keycloak_issuer_url` | keycloak issuer (required with `cert_api`) |
| `cert_api_keycloak_client_id` | keycloak client id (required with `cert_api`) |
| `cert_api_keycloak_client_secret` | keycloak client secret (required with `cert_api`) |

## re-deploying an existing instance

re-running `setup.yml` (or `finalize.yml`) against a host that's already
configured is safe: anything already present on the host is left alone. so you
can re-ansible after a breaking change without re-supplying every `-e` var or
copying secrets back off the instance. specifically, on a re-run:

- the **cert credential** is left alone if the host already has one — either a
  google ACME account key (`secrets/certbot_private_key.json`, gitignored so the
  code sync's `git clean` leaves it in place) or a cert_api keycloak secret baked
  into `config.toml`. so you don't need any cert secret on your control machine
  to redeploy an already-configured instance.
- **`config.toml`** is left untouched if it exists, so the cert creds, domain,
  and other settings baked in at first setup are preserved.
- the **claim token** is left untouched if it exists.
- **root-owned pixi/repo trees are reclaimed up front.** if a bad self-update
  left root-owned files in `/home/host/openhost` or `/home/host/.pixi` (which
  bricks the service — the host-user `pixi run` fails with EACCES), the run
  chowns them back to `host` *before* the `git`/`pixi install` steps that would
  otherwise choke on them. so a plain re-run recovers a bricked instance with no
  manual `chown`. this is what an existing user runs to fix the update button
  after upgrading from a pre-failsafe (pre-2026-07-08) checkout.

on a **first** deploy the host has no cert credential yet, so the run honors the
`cert_provider` flag: cert_api bakes the keycloak secret into `config.toml`,
otherwise the google ACME key is pushed from your control machine. a genuinely
missing ACME key fails the run there — as it should, since it's actually needed.

pass `-e overwrite_existing=true` to override all of the above and overwrite
with freshly rendered values — you'll need to re-supply the relevant vars (cert
creds, etc.) and have the ACME key locally when you do. use this when a breaking
change requires the `config.toml` template itself to be re-rendered.

## after setup

```bash
# check service status
ssh host@<IP> systemctl status openhost

# view logs
ssh host@<IP> journalctl -u openhost -f

# verify
curl https://<domain>/health
```
