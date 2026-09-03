# Deploying on a cloud instance

Use this guide if your machine has a static public IPv4 and can be dedicated to running Cloud in a Bottle.

If your machine is at home, it likely doesn't have a static IP. See [Deploying on a dedicated home server](./dedicated_homeserver.md) or [Deploying on a shared home machine](./shared_homeserver.md) instead.

## Prerequisites

- **A domain name you control**, and access to its DNS settings at your registrar or DNS provider. The examples use `mycooldomain.com`. You can use a subdomain of a domain you already own (`bottle.mycooldomain.com`); everything below works the same, just substitute it throughout.
- A machine with a **static public IPv4** (the examples use `203.0.113.10`), running a freshly installed **Ubuntu 24.04**.
- SSH access as `root`, or as a user with sudo.
- Inbound `80/tcp`, `443/tcp`, and `53/tcp+udp` reachable from the internet. 53 is required because instance runs its own authoritative DNS server. Some cloud providers put a firewall or security group in front of the machine by default, so make sure these ports are unblocked there.
- A root filesystem supporting idmapped mounts (ext4, xfs, or btrfs). This is standard; install fails early with a clear error if it does not.

---

## 1. Delegate DNS to the machine

Cloud in a Bottle runs an authoritative DNS server for your zone. It serves the wildcard `*.mycooldomain.com`, so every app gets a subdomain without you touching DNS again, and it answers the ACME DNS-01 challenge used to issue the wildcard TLS certificate.

So you delegate the whole zone to the machine rather than pointing an `A` record at it. For zone `mycooldomain.com` on a server at `203.0.113.10`, create two records at your DNS provider:

| Type | Name                   | Value                  |
|------|------------------------|------------------------|
| `A`  | `ns1.mycooldomain.com` | `203.0.113.10`         |
| `NS` | `mycooldomain.com`     | `ns1.mycooldomain.com` |

The `A` record is glue. An `NS` record can only name a host, not an IP, so something has to resolve `ns1` first.

Some registrars express this as "custom nameservers" for the domain rather than as an `NS` record you edit directly; either way you want `mycooldomain.com` delegated to `ns1.mycooldomain.com` at `203.0.113.10`.

## 2. Check the delegation

Do this before installing. Delegation changes can take anywhere from a minute to a day to propagate, depending on TTL on the previous record, and the install acquires a certificate over DNS-01, which will fail if the zone isn't yet delegated.

```bash
dig +short NS mycooldomain.com     # -> ns1.mycooldomain.com.
dig +short A ns1.mycooldomain.com  # -> 203.0.113.10
```

## 3. Install

SSH to the machine as root and run:

```bash
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain mycooldomain.com --acme-email you@example.com
```

The script:

1. creates an unprivileged `host` user and copies root's `authorized_keys` to it,
2. installs system packages, rootless Podman, and pixi,
3. clones Cloud in a Bottle to `/home/host/openhost` (openhost is the old name of this project),
4. writes config to `/home/host/.openhost/local_compute_space/`, detecting the machine's public IP for the DNS records CoreDNS will serve,
5. registers a Let's Encrypt account key (that's what `--acme-email` is for),
6. installs and starts the `openhost` systemd service.

## 4. Claim the instance

A publicly reachable instance is token-gated - otherwise the first stranger to find it could claim it. The installer prints a claim URL at the end of its output:

```
Claim URL: https://mycooldomain.com/setup?claim=<token>
```

Open it, create your owner account, and you're good to go! If the certificate is still being issued, give it a minute and reload.

## Debug / Manage / Upgrade

```bash
sudo systemctl status openhost
sudo journalctl -u openhost -f
```

The service runs as the unprivileged `host` user, with code at `/home/host/openhost` and config and data under `/home/host/.openhost/local_compute_space/`. Upgrades are a button on the dashboard's settings page. If you want to poke around further, see [Debugging](../operation/debugging.md) and [The bottle CLI](../operation/cli.md).
