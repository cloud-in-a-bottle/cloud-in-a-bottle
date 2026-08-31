# Deploying on a dedicated machine

Use this path when the machine runs nothing but Cloud in a Bottle: a VPS or other cloud server, or a spare machine at home.

Cloud in a Bottle installs directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. If you want to install on a non-dedicated machine, [install into a VM instead](./shared_machine.md).

The result is a public instance with HTTPS: a dashboard at `https://host.example.com/`, and each app on its own subdomain at `https://<app>.host.example.com/`.

## Prerequisites

- Ubuntu 24.04 on the target machine, freshly installed.
- SSH access as `root`, or as a user with sudo.
- A domain you control and can edit DNS for. You will give a subdomain zone to the instance. The examples use `host.example.com`.
- Inbound `80/tcp`, `443/tcp`, and `53/tcp+udp` reachable from the internet. Port 53 is required, because the instance runs its own DNS server.
- A root filesystem supporting idmapped mounts (ext4, xfs, or btrfs). Install fails early with a clear error if it does not.

On a VPS you get the last two for free. On a machine at home your IP is probably dynamic and your router is not forwarding anything yet, so read [Exposing a home server](./home_network.md) alongside step 1. Everything else here is the same.

## 1. Delegate DNS to the machine

Cloud in a Bottle runs an authoritative DNS server for your zone. It serves the wildcard `*.host.example.com`, so every app gets a subdomain without you touching DNS again, and it answers the ACME DNS-01 challenge used to issue the wildcard TLS certificate.

So you delegate the whole zone to the machine rather than pointing an `A` record at it. For zone `host.example.com` on a server at `203.0.113.10`, create two records at your DNS provider:

| Type | Name                   | Value                  |
|------|------------------------|------------------------|
| `A`  | `ns1.host.example.com` | `203.0.113.10`         |
| `NS` | `host.example.com`     | `ns1.host.example.com` |

The `A` record is glue. An `NS` record can only name a host, not an IP, so something has to resolve `ns1` first.

Check the delegation before continuing. It can take a while to propagate.

```bash
dig +short NS host.example.com     # -> ns1.host.example.com.
```

The instance now answers for everything at or below `host.example.com`. You never create per-app records.

## 2. Install

SSH to the machine as root and run:

```bash
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain host.example.com
```

The script:

1. creates an unprivileged `host` user and copies root's `authorized_keys` to it,
2. installs system packages, rootless Podman, and pixi,
3. clones Cloud in a Bottle to `/home/host/openhost`,
4. writes config to `/home/host/.openhost/local_compute_space/`,
5. registers an ACME account for certificate issuance,
6. installs and starts the `openhost` systemd service.

Expect about ten minutes on a fresh image. Most of that is `apt upgrade`.

On first start the service gets a wildcard certificate covering both `host.example.com` and `*.host.example.com` over DNS-01, then starts Caddy on port 443. One certificate covers every app, so installing an app never triggers a new certificate.

## 3. Claim the instance

The run prints a claim URL near the end of its output:

```
Claim URL: https://host.example.com/setup?claim=<token>
```

Open it and set the owner username and password.

Until the instance is claimed, every request redirects to a gated `/setup`, and `/setup` rejects anyone without the token. On a public instance that token is the only thing stopping a stranger from claiming it, so treat it as a secret and claim promptly.

If you lose the URL, read the token off the machine:

```bash
sudo cat /home/host/.openhost/local_compute_space/first_boot.toml
```

## Verify and manage

```bash
curl https://host.example.com/health        # -> {"status":"ok"}

sudo systemctl status openhost
sudo journalctl -u openhost -f
```

| What                | Where                                        |
|---------------------|----------------------------------------------|
| Service             | `openhost` (systemd)                          |
| Code                | `/home/host/openhost`                         |
| Config              | `/home/host/.openhost/local_compute_space/config.toml` |
| Persistent app data | `/home/host/.openhost/local_compute_space/`   |
| Runs as             | the unprivileged `host` user                  |

To upgrade to a newer Cloud in a Bottle release, use the update button on the dashboard's settings page. It pulls new code, syncs dependencies, and restarts the service.

## Installing before DNS is ready

If the machine is up but the domain is not, add `--local-http-only`:

```bash
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain host.example.com --local-http-only
```

This skips TLS, CoreDNS, and Caddy. The router serves plain HTTP on port 8080, bound to loopback. Reach the dashboard over an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 host@203.0.113.10     # then open http://localhost:8080
```

Once the DNS records from step 1 are live, switch to TLS by discarding the HTTP-only config and re-running without the flag:

```bash
sudo rm /home/host/.openhost/local_compute_space/config.toml
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain host.example.com
```

Removing `config.toml` is what forces it to be rewritten. The installer preserves an existing one, so the HTTP-only settings would otherwise stick. Your data, secrets, and claim state live elsewhere and are untouched.
