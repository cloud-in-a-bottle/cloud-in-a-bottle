# Deploying on a shared home machine

Use this path when you want to run Cloud in a Bottle on a machine at home that you use for other things too: your desktop, a NAS, etc.

Cloud in a Bottle wants to install directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. So instead of installing on the host, you give it its own Ubuntu VM to live inside. Everything it does stays inside the VM's disk image, and it can't access your host system. If you're happy to dedicate the whole machine to it, [see this guide instead](./dedicated_homeserver.md); if it's a VPS or cloud server, see [Deploying on a cloud instance](./cloud_instance.md).

This page is in two parts. Part 1 gets a working instance running inside a VM from our pre-built image. [Part 2](#part-2-taking-it-public) covers the networking and config if you want to put it on the public internet.

## Part 1: download and run the VM image

Requirements:
- support for hardware virtualization. Most CPUs support this as long as you're running on bare metal, ie not already in a VM (VPS, EC2 instance, etc). It'll work without this but would be very slow.
- a virtual machine host, like QEMU, VirtualBox, VMWare, etc. If you don't already have a preference, we suggest QEMU.
  - on ubuntu: `apt install qemu-system-x86 qemu-utils` (x86-64), or `apt install qemu-system-arm qemu-efi-aarch64 qemu-utils` (arm64)
  - on an Apple silicon Mac: [UTM](https://mac.getutm.app/), or `brew install qemu`

The release image is a self-contained Ubuntu 24.04 appliance with Cloud in a Bottle already setup. These files are published per release:

| File           | Use with                                                 |
| -------------- | -------------------------------------------------------- |
| `-amd64.qcow2` | QEMU / KVM / libvirt (`virt-manager`) on x86-64          |
| `-amd64.ova`   | VirtualBox (and most other hypervisors) on x86-64        |
| `-arm64.qcow2` | QEMU / KVM / libvirt or UTM on arm64; boots through UEFI |

Grab the latest version from the [releases page](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/releases).

### Boot it

Give the VM at least 1 vCPU, 2 GB RAM, and a disk of the size you want your instance to have (min 20GB). The root filesystem grows to fill it on first boot, so a 60 GB disk yields ~58 GB of usable space.

- **VirtualBox:** *File → Import Appliance…*, select the `.ova`, adjust CPU/RAM/disk, and start it.
- **QEMU / libvirt:** import the `.qcow2` as the VM's disk (e.g. `virt-manager`'s "Import existing disk image"), or boot it directly:

x86 QEMU instructions: 
```bash
qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 2 -m 4096 \
  -drive file=openhost-<version>-amd64.qcow2,format=qcow2,if=virtio \
  -netdev user,id=n0,hostfwd=tcp::8080-:8080,hostfwd=tcp::2222-:22 \
  -device virtio-net-pci,netdev=n0 \
  -nographic
```

On an arm64 machine the equivalent is below. The image boots through UEFI, so `-bios` points at the edk2 firmware: `/usr/share/qemu-efi-aarch64/QEMU_EFI.fd` from `qemu-efi-aarch64` on Ubuntu, or `$(brew --prefix)/share/qemu/edk2-aarch64-code.fd` with Homebrew. On a Mac, swap `-enable-kvm` for `-accel hvf`.

```bash
qemu-system-aarch64 -enable-kvm -machine virt -cpu host -smp 2 -m 4096 \
  -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  -drive file=openhost-<version>-arm64.qcow2,format=qcow2,if=virtio \
  -netdev user,id=n0,hostfwd=tcp::8080-:8080,hostfwd=tcp::2222-:22 \
  -device virtio-net-pci,netdev=n0 \
  -nographic
```

In UTM, create a new *Virtualize* VM of type *Linux*, choose to import an existing drive, and select the `.qcow2`.

The `hostfwd` options make the VM reachable. QEMU's default networking puts the guest on an isolated NAT with no address you can browse to, so instead we forward the guest's `:8080` and `:22` to `:8080` and `:2222` on the machine running QEMU.

First boot runs `openhost-prepare.service` before the dashboard comes up; give it a minute.

The local VM console logs in as user `host` with password `openhost` (change it with `passwd`). To get SSH access, log in on the console and append your public key as that `host` user:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "ssh-ed25519 AAAAC3Nz... you@yourmachine" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Use the contents of `~/.ssh/id_ed25519.pub` from the machine you'll connect from, not the placeholder above. If you don't have one yet, run `ssh-keygen -t ed25519` there first.

### Access it

Point a browser at `http://<vm-ip>:8080/`, or `http://localhost:8080/` if you booted with the `hostfwd` line above. Create your owner account and you're good to go!

### Reaching your apps

Cloud in a Bottle routes to apps by subdomain: an app named `foo` lives at `foo.<domain>`. The appliance has no real domain, so it uses [`lvh.me`](https://lvh.me), a public convenience domain where both `lvh.me` and `*.lvh.me` resolve to `127.0.0.1`.

`lvh.me` resolves to the `127.0.0.1` of whatever machine is doing the browsing, so how you reach it depends on where your browser is.

**Browsing on the machine running QEMU:** the `hostfwd` above is all you need, no SSH tunnel. The dashboard is at `http://lvh.me:8080/` and an app named `foo` at `http://foo.lvh.me:8080/`.
**Browsing from another machine** (the VM lives on a NAS, you're on a laptop): forward the appliance's `:8080` to a local port over SSH. (`ssh -L 8088:localhost:8080 <you>@<qemu-host>`)

That goes through your normal account on the machine running QEMU, so it needs no key on the appliance itself. If instead the VM has its own reachable address (bridged networking, or VirtualBox), you can tunnel straight to it with `ssh -L 8088:localhost:8080 host@<vm-ip>` once you've added your key.

Either way, browse to the dashboard at `http://lvh.me:8088/` and an app named `foo` at `http://foo.lvh.me:8088/`. You can pick whatever local port you like.

## Part 2: taking it public

Follow [Exposing a home server](./home_network.md) for a typical home ISP connection, or [Exposing a server with a static IP](./static_ip.md) if you have a static IP.
