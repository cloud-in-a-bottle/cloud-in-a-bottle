# Deploying on a dedicated home server

Use this path when you have a machine at home that runs nothing but Cloud in a Bottle: a spare desktop, a mini PC, an old laptop.

Cloud in a Bottle installs directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. If you don't want to give it the whole machine, [install into a VM instead](./shared_homeserver.md). If the machine is a VPS or cloud server rather than something on your home network, [deploy on a cloud instance](./cloud_instance.md) instead. A static public IP lets you install straight onto your domain and skip the local-only stage entirely.

This page is in two parts. [Part 1](#part-1-core-instance-setup) gets a working instance running on the machine. [Part 2](#part-2-taking-it-public) covers the networking needed to put it on the public internet.

The end state is a public instance with HTTPS: a dashboard at `https://mycooldomain.com/`, and each app on its own subdomain at `https://<app>.mycooldomain.com/`.

## Prerequisites

- Ubuntu 24.04 on the target machine, freshly installed.
- SSH access as `root`, or as a user with sudo.
- A root filesystem supporting idmapped mounts (ext4, xfs, or btrfs). Install fails early with a clear error if it does not.

---

## Part 1: core instance setup

### 1. Install

Install in local HTTP-only mode. This needs nothing from the network: it skips TLS, CoreDNS, and Caddy, and the router serves plain HTTP on port 8080 bound to loopback. Part 2 turns on the public, HTTPS-serving configuration once your networking is sorted out.

SSH to the machine as root and run:

```bash
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | bash -s -- --domain lvh.me:8080 --local-http-only --open-claim
```

The script:

1. creates an unprivileged `host` user and copies root's `authorized_keys` to it,
2. installs system packages, rootless Podman, and pixi,
3. clones Cloud in a Bottle to `/home/host/openhost` (openhost is the old name of this project),
4. writes config to `/home/host/.openhost/local_compute_space/`,
5. installs and starts the `openhost` systemd service.

Expect about ten minutes on a fresh image.

### 2. Reach the dashboard

Port 8080 is bound to loopback, so tunnel to it over SSH:

```bash
ssh -L 8080:localhost:8080 host@<machine-ip>
```

Then open `http://lvh.me:8080` and setup your instance!

`lvh.me` is a public DNS name that simply resolves to `127.0.0.1`. Similar to `localhost`, but subdomains (`mycoolapp.lvh.me`) also resolve properly.

You'll need to have that SSH tunnel active to access your instance until you take it public.

### Debug / Manage / Upgrade

```bash
sudo systemctl status openhost
sudo journalctl -u openhost -f
```

The service runs as the unprivileged `host` user, with code at `/home/host/openhost` and config and data under `/home/host/.openhost/local_compute_space/`. Upgrades are a button on the dashboard's settings page. If you want to poke around further, see [Debugging](../operation/debugging.md) and [The bottle CLI](../operation/cli.md).


## Part 2: taking it public

The point of a Bottle instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, reachable from an ordinary browser on any device. That needs the machine to be reachable from the internet at your domain.

There are two paths, and which one applies is decided by your network.

### Option A: a home network, the usual case

At home you typically have no static IP, and your router is not forwarding anything yet. Sometimes you have no usable public IP at all, because your ISP puts you behind CGNAT. The workarounds have real tradeoffs.

See [Exposing a home server](./home_network.md) for the options: an HTTP(s) tunnel such as Cloudflare Tunnels, a forthcoming IPv4 tunnel service, or port-forwarding a dynamic ISP address.

Nothing in part 1 changes; the instance is already running and reachable over the tunnel. Pick a path from that page to give it a public address.

### Option B: a static, public IP

If your connection does give you a static public IP and you can open ports 53, 80, and 443 to the machine, you get the simpler path: delegate a DNS zone to it, then re-run the installer with TLS on.

See [Exposing a server with a static IP](./static_ip.md).
