# Deploying on a dedicated machine

Use this path when the machine runs nothing but Cloud in a Bottle: a VPS or other cloud server, or a spare machine at home.

Cloud in a Bottle installs directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. If you want to install on a non-dedicated machine, [install into a VM instead](./shared_machine.md).

This page is in two parts. [Part 1](#part-1-core-instance-setup) gets a working instance running on the machine. [Part 2](#part-2-taking-it-public) covers the networking needed to put it on the public internet.

The end state is a public instance with HTTPS: a dashboard at `https://host.example.com/`, and each app on its own subdomain at `https://<app>.host.example.com/`.

## Prerequisites

- Ubuntu 24.04 on the target machine, freshly installed.
- SSH access as `root`, or as a user with sudo.
- A root filesystem supporting idmapped mounts (ext4, xfs, or btrfs). Install fails early with a clear error if it does not.

---

# Part 1: core instance setup

## Choosing an install mode

The installer has two modes, and which one you want depends on whether the machine's public networking is ready yet.

**Public mode** (the default) issues a wildcard TLS certificate on first start and serves HTTPS on port 443. Getting that certificate requires an ACME DNS-01 challenge, which requires the DNS delegation from part 2 to already be live. So use this mode only if you have a static IP and have already done [Delegate DNS to the machine](#option-a-a-static-public-ip).

**Local HTTP-only mode** (`--local-http-only`) skips TLS, CoreDNS, and Caddy entirely. The router serves plain HTTP on port 8080 bound to loopback, and you reach it over an SSH tunnel. Use this if the domain is not ready yet, if you are going the tunnel route, or if you just want the instance up now and will [switch to TLS later](#switching-from-local-http-only-to-tls).

If you are unsure, start with `--local-http-only`. Switching to TLS afterwards is a one-command change and does not touch your data or claim state.

## 1. Install

SSH to the machine as root and run one of:

```bash
# public mode -- requires DNS delegation to already be live (part 2)
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain host.example.com
```

```bash
# local HTTP-only mode -- no public networking needed
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain host.example.com --local-http-only
```

`--domain` is required in both cases. In local HTTP-only mode it is only used for app subdomain routing, not for TLS or DNS.

The script:

1. creates an unprivileged `host` user and copies root's `authorized_keys` to it,
2. installs system packages, rootless Podman, and pixi,
3. clones Cloud in a Bottle to `/home/host/openhost`,
4. writes config to `/home/host/.openhost/local_compute_space/`,
5. registers an ACME account for certificate issuance,
6. installs and starts the `openhost` systemd service.

Expect about ten minutes on a fresh image. Most of that is `apt upgrade`.

In public mode, first start also gets a wildcard certificate covering both `host.example.com` and `*.host.example.com` over DNS-01, then starts Caddy on port 443. One certificate covers every app, so installing an app never triggers a new certificate.

## 2. Reach the dashboard

In public mode, the dashboard is at `https://host.example.com/`.

In local HTTP-only mode, tunnel to it over SSH:

```bash
ssh -L 8080:localhost:8080 host@203.0.113.10     # then open http://localhost:8080
```

## 3. Claim the instance

The install run prints a claim URL near the end of its output:

```
Claim URL: https://host.example.com/setup?claim=<token>
```

Open it and set the owner username and password. In local HTTP-only mode, use the same `?claim=<token>` query against your tunnelled `http://localhost:8080/setup`.

Until the instance is claimed, every request redirects to a gated `/setup`, and `/setup` rejects anyone without the token. On a public instance that token is the only thing stopping a stranger from claiming it, so treat it as a secret and claim promptly.

If you lose the URL, read the token off the machine:

```bash
sudo cat /home/host/.openhost/local_compute_space/first_boot.toml
```

## Verify and manage

```bash
curl https://host.example.com/health        # -> {"status":"ok"}   (or http://localhost:8080/health via the tunnel)

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

At this point you have a fully working instance — you can install apps and use it. What it does not have yet, unless you installed in public mode, is a public address.

---

# Part 2: taking it public

The point of a Bottle instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, reachable from an ordinary browser on any device. That needs the machine to be reachable from the internet at your domain.

There are two paths, and which one applies is decided by your network, not by preference.

## Option A: a static, public IP

This is the case on essentially any VPS or cloud server, and it is the simpler path. You need:

- a static public IP for the machine (the examples use `203.0.113.10`),
- inbound `80/tcp`, `443/tcp`, and `53/tcp+udp` reachable from the internet. Port 53 is required, because the instance runs its own DNS server.

### Delegate DNS to the machine

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

### Switching from local HTTP-only to TLS

If you installed with `--local-http-only`, switch to TLS once the DNS records above are live by discarding the HTTP-only config and re-running the installer without the flag:

```bash
sudo rm /home/host/.openhost/local_compute_space/config.toml
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain host.example.com
```

Removing `config.toml` is what forces it to be rewritten. The installer preserves an existing one, so the HTTP-only settings would otherwise stick. Your data, secrets, and claim state live elsewhere and are untouched.

## Option B: a home network

At home you typically have no static IP, and your router is not forwarding anything yet. Sometimes you have no usable public IP at all, because your ISP puts you behind CGNAT. Option A does not apply, and the workarounds have real tradeoffs.

See [Exposing a home server](./home_network.md) for the options — an HTTP(s) tunnel such as Cloudflare Tunnels, a forthcoming IPv4 tunnel service, or port-forwarding a dynamic ISP address.

Everything in part 1 is unchanged on a home machine; install with `--local-http-only` and pick a path from that page.
