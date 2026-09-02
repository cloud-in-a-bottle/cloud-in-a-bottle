# Deploying on a shared machine

Use this path when installing on a machine that you don't necessarily want to dedicate to Cloud in a Bottle.

Cloud in a Bottle wants to install directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. So instead of installing on the host, you give it its own Ubuntu VM to live inside. Everything it does stays inside the VM's disk image, and it can't access your host system. If you want to install on a dedicated machine, [see this guide instead](./dedicated_machine.md).

This page is in two parts. Part 1 gets a working instance running inside a VM from our pre-built image. [Part 2](#part-2-taking-it-public) covers the networking and config needed to put it on the public internet at your own domain.

## Part 1: download and run the VM image

The release image is a self-contained Ubuntu 24.04 appliance with Cloud in a Bottle already provisioned by the same `scripts/provision.sh` a production deploy uses. It is deliberately configured for the easy on-ramp:

- **HTTP only, no domain required.** The router binds `0.0.0.0:8080` with no TLS, CoreDNS, or Caddy in front of it. Boot the VM and the dashboard is at `http://<vm-ip>:8080/`.
- **No claim token** There is no claim token to copy around — the first person to reach the dashboard claims the instance. This is safe only because the VM sits behind your NAT: nothing reaches `:8080` unless you forward a port to it yourself.
- **No baked-in SSH key.** Access is console-only until you add your own key. The image ships no shared credentials.
- **Grow-to-fill disk.** On first boot a systemd oneshot (`openhost-prepare.service`) expands the root filesystem to fill whatever disk you gave the VM (20 GB floor), regenerates the SSH host keys, and assigns a fresh machine-id — so every install is distinct.

Two formats are published per release, both x86_64:

| File     | Use with                                |
| -------- | --------------------------------------- |
| `.qcow2` | QEMU / KVM / libvirt (`virt-manager`)   |
| `.ova`   | VirtualBox (and most other hypervisors) |

### Download

Grab the latest `.qcow2` or `.ova` from the [releases page](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/releases). Pick the format that matches your hypervisor.

### Boot it

Give the VM at least **2 vCPU, 4 GB RAM**, and a disk of the size you want your instance to have — the root filesystem grows to fill it on first boot, so a 60 GB disk yields ~58 GB of usable space. The 20 GB floor is the minimum; go bigger if you plan to run several apps.

- **VirtualBox:** *File → Import Appliance…*, select the `.ova`, adjust CPU/RAM/disk, and start it.
- **QEMU / libvirt:** import the `.qcow2` as the VM's disk (e.g. `virt-manager`'s "Import existing disk image"), or boot it directly with `qemu-system-x86_64`.

First boot runs `openhost-prepare.service` before the dashboard comes up; give it a minute. If you need a shell, log in on the VM console and add your SSH public key to `~/.ssh/authorized_keys`.

### Claim it

Point a browser at `http://<vm-ip>:8080/`. Claiming is open, so the dashboard lets you claim the instance immediately — no token. Create your owner account and you're in.

> Keep the VM behind your NAT/firewall until you've claimed it. Open claiming means anyone who can reach `:8080` before you do can take the instance. Don't port-forward `:8080` to the public internet on an unclaimed appliance.

### Reaching your apps (subdomains without a domain)

Cloud in a Bottle routes to apps **by subdomain** — an app named `foo` lives at `foo.<domain>`. The appliance has no real domain, so it uses [`lvh.me`](https://lvh.me), a public convenience domain where both `lvh.me` and `*.lvh.me` resolve to `127.0.0.1`. That gives you working wildcard subdomains without registering anything.

Forward the appliance's `:8080` to a local port over SSH (once you've added your key):

```bash
ssh -L 8088:localhost:8080 host@<vm-ip>
```

Then browse to the dashboard at `http://lvh.me:8088/` and an app named `foo` at `http://foo.lvh.me:8088/`. The router carries the tunnel port through every absolute link and redirect it builds, so the `/login` bounce and the app links land on `lvh.me:8088` rather than dropping back to `:80`. You can pick whatever local port you like.

## Part 2: taking it public

The released images are HTTP-only; To serve an instance at your own domain from a VM, you will need to **build your own image** with the domain baked in and HTTPS turned on — see [Building your own VM image](./building_a_vm_image.md).

From there, putting it on the internet is the same networking as any install: delegate a DNS zone to the VM's public IP and open ports 53, 80, and 443 to it. See [Exposing a server with a static IP](./static_ip.md) for a VPS/static-IP host, or [Exposing a home server](./home_network.md) for a home connection.
