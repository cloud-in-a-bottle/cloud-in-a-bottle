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

### Networking: bridged

Give the VM **bridged** networking. The VM then joins your home network as its own machine, with its own address from your router, just like a dedicated home server would. Apps that talk to devices on your network (eg Home Assistant, which discovers and controls smart-home devices) need this. Under the hypervisor's default NAT networking the VM sits behind the host and can't see those devices.

Bridging generally needs the host to be on **wired Ethernet**: most Wi-Fi access points only accept traffic from the host's own hardware address, so a VM bridged over Wi-Fi either gets no connection or loses the multicast traffic that device discovery uses. If your host is on Wi-Fi, see [NAT networking](#fallback-nat-networking) below.

### Boot it

Give the VM at least 1 vCPU, 2 GB RAM, and a disk of the size you want your instance to have (min 20GB). The root filesystem grows to fill it on first boot, so a 60 GB disk yields ~58 GB of usable space. Give it more RAM if you plan to run heavier apps (Home Assistant alone wants about 1 GB).

- **VirtualBox:** *File → Import Appliance…*, select the `.ova`, and adjust CPU/RAM/disk. Before starting it, open *Settings → Network → Adapter 1*, set *Attached to* to *Bridged Adapter*, and pick the host's Ethernet interface.
- **UTM (Mac):** create a new *Virtualize* VM of type *Linux*, choose to import an existing drive, and select the `.qcow2`. In the VM's settings, set *Network → Network Mode* to *Bridged (Advanced)* and pick the Ethernet interface.
- **Proxmox:** import the `.qcow2` as the VM's disk. VMs attach to `vmbr0` by default, which is already bridged to your network.
- **libvirt (`virt-manager`):** "Import existing disk image" with the `.qcow2`. For the NIC, choose *Bridge device* with the host bridge from the QEMU instructions below (eg `br0`). *Macvtap device* on the host's Ethernet interface also works without setting up a bridge, but the host itself then can't reach the VM (other machines on your network can).
- **QEMU:** see below.

#### QEMU on a Linux host

First turn the host's Ethernet interface into a bridge. On Ubuntu, write `/etc/netplan/01-br0.yaml`, replacing `enp3s0` with your interface name (`ip link` lists them):

```yaml
network:
  version: 2
  ethernets:
    enp3s0: { dhcp4: false }
  bridges:
    br0:
      interfaces: [enp3s0]
      dhcp4: true
```

Apply it with `sudo netplan apply`. The host's address moves from `enp3s0` to `br0`, so do this from a local console rather than over SSH. Then let QEMU attach VMs to the bridge:

```bash
sudo mkdir -p /etc/qemu
echo "allow br0" | sudo tee -a /etc/qemu/bridge.conf
sudo chmod u+s /usr/lib/qemu/qemu-bridge-helper
```

x86 QEMU instructions:
```bash
qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 2 -m 4096 \
  -drive file=cloud-in-a-bottle-<version>-amd64.qcow2,format=qcow2,if=virtio \
  -netdev bridge,id=n0,br=br0 \
  -device virtio-net-pci,netdev=n0 \
  -nographic
```

On an arm64 machine the equivalent is below. The image boots through UEFI, so `-bios` points at the edk2 firmware: `/usr/share/qemu-efi-aarch64/QEMU_EFI.fd` from `qemu-efi-aarch64` on Ubuntu.

```bash
qemu-system-aarch64 -enable-kvm -machine virt -cpu host -smp 2 -m 4096 \
  -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  -drive file=cloud-in-a-bottle-<version>-arm64.qcow2,format=qcow2,if=virtio \
  -netdev bridge,id=n0,br=br0 \
  -device virtio-net-pci,netdev=n0 \
  -nographic
```

#### QEMU on a Mac

Homebrew's QEMU can bridge through macOS's vmnet framework, which needs root. Replace `en0` with your Ethernet interface (`networksetup -listallhardwareports` lists them):

```bash
sudo qemu-system-aarch64 -accel hvf -machine virt -cpu host -smp 2 -m 4096 \
  -bios $(brew --prefix)/share/qemu/edk2-aarch64-code.fd \
  -drive file=cloud-in-a-bottle-<version>-arm64.qcow2,format=qcow2,if=virtio \
  -netdev vmnet-bridged,id=n0,ifname=en0 \
  -device virtio-net-pci,netdev=n0 \
  -nographic
```

### First boot

First boot runs `bottle-prepare.service` before the dashboard comes up; give it a minute.

The local VM console logs in as user `host` with password `cloudinabottle` (change it with `passwd`). SSH only accepts keys, so to get SSH access, log in on the console and append your public key as that `host` user:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "ssh-ed25519 AAAAC3Nz... you@yourmachine" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Use the contents of `~/.ssh/id_ed25519.pub` from the machine you'll connect from, not the placeholder above. If you don't have one yet, run `ssh-keygen -t ed25519` there first.

While you're on the console, `ip -4 addr` shows the VM's address on your network (`<vm-ip>` below). It should be in the same range as your other devices, eg `192.168.1.x`. It's worth giving the VM a DHCP reservation in your router so that address doesn't change.

### Access it

Point a browser at `http://<vm-ip>:8080/`, create your owner account, and you're good to go!

**Do this straight away.** Until the instance is claimed, the setup page is open to anyone who can reach it, and with bridged networking that's every device on your network.

Also check that your router blocks inbound IPv6 connections from the internet (most do by default). A bridged VM usually gets a public IPv6 address from your router, which would otherwise make its SSH port reachable from the internet.

### Reaching your apps

Cloud in a Bottle routes to apps by subdomain: an app named `foo` lives at `foo.<domain>`. The appliance has no real domain, so it uses [`lvh.me`](https://lvh.me), a public convenience domain where both `lvh.me` and `*.lvh.me` resolve to `127.0.0.1`.

`lvh.me` resolves to the `127.0.0.1` of whatever machine is doing the browsing, so forward a local port to the VM over SSH (this needs the key you added above):

```bash
ssh -L 8088:localhost:8080 host@<vm-ip>
```

Then browse to the dashboard at `http://lvh.me:8088/` and an app named `foo` at `http://foo.lvh.me:8088/`. You can pick whatever local port you like. Once you [take it public](#part-2-taking-it-public), apps get real URLs and the tunnel isn't needed.

### Fallback: NAT networking

If you can't bridge (eg the host is on Wi-Fi), the hypervisor's default NAT networking works for everything except apps that need to reach devices on your network. Apps like Home Assistant can still talk to devices you add by IP address, but they can't discover devices automatically, and some devices can't send updates back to them.

NAT puts the guest on an isolated network with no address you can browse to, so forward the guest's `:8080` and `:22` to ports on the host. In QEMU, swap the `-netdev` line above for:

```bash
  -netdev user,id=n0,hostfwd=tcp::8080-:8080,hostfwd=tcp::2222-:22 \
```

(QEMU on a Mac also doesn't need `sudo` with this one.) In VirtualBox and UTM, leave the network on NAT / *Shared Network* and add the same port forwards in the network settings.

**Browsing on the host:** the forwards are all you need, no SSH tunnel. The dashboard is at `http://lvh.me:8080/` and an app named `foo` at `http://foo.lvh.me:8080/`.

**Browsing from another machine** (the VM lives on a NAS, you're on a laptop): forward a local port to the host's `:8080` over SSH (`ssh -L 8088:localhost:8080 <you>@<qemu-host>`), then browse to `http://lvh.me:8088/`. That goes through your normal account on the host, so it needs no key on the appliance itself.

## Part 2: taking it public

Follow [Exposing a home server](./home_network.md) for a typical home ISP connection, or [Exposing a server with a static IP](./static_ip.md) if you have a static IP.
