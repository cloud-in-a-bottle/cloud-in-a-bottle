# Deploying on a shared home machine

Use this path when you want to run Cloud in a Bottle on a machine at home that you use for other things too: your desktop, a NAS, etc.

Cloud in a Bottle wants to install directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. So instead of installing on the host, you give it its own Ubuntu VM to live inside. Everything it does stays inside the VM's disk image, and it can't access your host system. If you're happy to dedicate the whole machine to it, [see this guide instead](./dedicated_homeserver.md); if it's a VPS or cloud server, see [Deploying on a cloud instance](./cloud_instance.md).

This page is in two parts. Part 1 gets a working instance running inside a VM from our pre-built image. [Part 2](#part-2-taking-it-public) covers the networking and config if you want to put it on the public internet.

## Part 1: download and run the VM image

The release image is a self-contained Ubuntu 24.04 appliance with Cloud in a Bottle already setup. It is deliberately configured for the easy on-ramp:
- **HTTP only, no domain required.** The router binds `0.0.0.0:8080`. Boot the VM and the dashboard is at `http://<vm-ip>:8080/`.
- **Grow-to-fill disk.** On first boot a systemd oneshot (`openhost-prepare.service`) expands the root filesystem to fill whatever disk you gave the VM (20 GB floor), regenerates the SSH host keys, and assigns a fresh machine-id, so every install is distinct.

Two formats are published per release, both x86_64:

| File     | Use with                                |
| -------- | --------------------------------------- |
| `.qcow2` | QEMU / KVM / libvirt (`virt-manager`)   |
| `.ova`   | VirtualBox (and most other hypervisors) |

Grab the latest version from the [releases page](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/releases).

### Boot it

Give the VM at least **2 vCPU, 4 GB RAM**, and a disk of the size you want your instance to have. The root filesystem grows to fill it on first boot, so a 60 GB disk yields ~58 GB of usable space.

- **VirtualBox:** *File → Import Appliance…*, select the `.ova`, adjust CPU/RAM/disk, and start it.
- **QEMU / libvirt:** import the `.qcow2` as the VM's disk (e.g. `virt-manager`'s "Import existing disk image"), or boot it directly with `qemu-system-x86_64`.

First boot runs `openhost-prepare.service` before the dashboard comes up; give it a minute. If you want SSH access, log in on the VM console and add your SSH public key to `~/.ssh/authorized_keys`.

### Access it it

Point a browser at `http://<vm-ip>:8080/`. Create your owner account and you're good to go!

### Reaching your apps

Cloud in a Bottle routes to apps **by subdomain**: an app named `foo` lives at `foo.<domain>`. The appliance has no real domain, so it uses [`lvh.me`](https://lvh.me), a public convenience domain where both `lvh.me` and `*.lvh.me` resolve to `127.0.0.1`. That gives you working wildcard subdomains without registering anything.

Forward the appliance's `:8080` to a local port over SSH (once you've added your key):

```bash
ssh -L 8088:localhost:8080 host@<vm-ip>
```

Then browse to the dashboard at `http://lvh.me:8088/` and an app named `foo` at `http://foo.lvh.me:8088/`. You can pick whatever local port you like.

## Part 2: taking it public

Follow [Exposing a home server](./home_network.md) for a typical home ISP connection, or [Exposing a server with a static IP](./static_ip.md) if you have a static IP.
