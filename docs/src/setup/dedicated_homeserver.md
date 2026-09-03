# Deploying on a dedicated home server

Use this path when you have a machine at home that can just run Cloud in a Bottle: a spare desktop, a raspberry pi, an old laptop.

Cloud in a Bottle installs directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. If you don't want to give it the whole machine, [install into a VM instead](./shared_homeserver.md). If the machine is a VPS or cloud server rather than something on your home network, [deploy on a cloud instance](./cloud_instance.md) instead.

## Prerequisites

- Ubuntu 24.04 on the target machine, freshly installed.
- SSH access as `root`, or as a user with sudo.
- A root filesystem supporting idmapped mounts (ext4, xfs, or btrfs). Install fails early with a clear error if it does not.

---

## Part 1: core instance setup

### 1. Install

SSH to the machine as root and run:

```bash
curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh \
  | sudo bash -s -- --domain lvh.me:8080 --local-http-only --open-claim
```

The script:

1. creates an unprivileged `host` user and copies root's `authorized_keys` to it,
2. installs system packages, rootless Podman, and pixi,
3. clones Cloud in a Bottle to `/home/host/openhost` (openhost is the old name of this project),
4. writes config to `/home/host/.openhost/local_compute_space/`,
5. installs and starts the `openhost` systemd service.

### 2. Reach the dashboard

Port 8080 is bound to loopback, so tunnel to it over SSH:

```bash
ssh -L 8080:localhost:8080 host@<machine-ip>
```

Then open `http://lvh.me:8080` and setup your instance!

`lvh.me` is a public DNS name that simply resolves to `127.0.0.1`. Similar to `localhost`, but subdomains (`mycoolapp.lvh.me`) also resolve properly.

You'll need to have that SSH tunnel active to access your instance until you take it public.

## Debug / Manage / Upgrade

```bash
sudo systemctl status openhost
sudo journalctl -u openhost -f
```

The service runs as the unprivileged `host` user, with code at `/home/host/openhost` and config and data under `/home/host/.openhost/local_compute_space/`. Upgrades are a button on the dashboard's settings page. If you want to poke around further, see [Debugging](../operation/debugging.md) and [The bottle CLI](../operation/cli.md).


## Part 2: taking it public

Follow [Exposing a home server](./home_network.md) for a typical home ISP connection, or [Exposing a server with a static IP](./static_ip.md) if you have a static IP.
