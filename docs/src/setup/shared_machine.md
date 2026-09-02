# Deploying on a shared machine

Use this path when installing on a machine that you don't necessarily want to dedicate to Cloud in a Bottle.

Cloud in a Bottle wants to install directly on the host. It runs various system services, sets system-level configuration, and expects to be able to manage the system ongoing. So instead of installing on the host, you give it its own Ubuntu VM to live inside. Everything it does stays inside the VM's disk image, and it can't access your host system. If you want to install on a dedicated machine, [see this guide instead](./dedicated_machine.md).

This guide starts by setting up a local-network-only instance. The local instance lives at `http://lvh.me:8080`, with apps at `http://<app>.lvh.me:8080`. `lvh.me` is a public DNS name that resolves to `127.0.0.1`, wildcard subdomains included. Cloud in a Bottle routes apps by subdomain, so this gives you working app URLs with no DNS setup on your part.

When you're ready to take your instance public, see [going public](#going-public) at the end of this doc.

## What the VM needs

Cloud in a Bottle does not care which hypervisor you use. QEMU, UTM, VirtualBox, VMware, Hyper-V, libvirt/virt-manager, Multipass, and Proxmox all work. The VM just has to provide:

- Ubuntu 24.04.
- Key-based SSH from your machine, as a user with sudo.
- The VM's port 8080 reachable at `127.0.0.1:8080` on your machine, via a port forward in the VM host's NAT config or an SSH tunnel.
- About 8 GB of RAM, 4 cores, and 40 GB of disk.
- An ext4, xfs, or btrfs root filesystem. App containers need idmapped mounts, and install fails early with a clear error otherwise.

[Part 1](#part-1-build-an-ubuntu-vm-with-qemu) is a QEMU recipe you can follow verbatim if you do not already have a preferred way to make VMs. Otherwise build the VM however you like and skip to [Part 2](#part-2-install-cloud-in-a-bottle-into-the-vm).

## On your machine

- Ansible: `uv tool install ansible-core`, or `pipx install ansible-core`. It runs on your machine, never inside the VM.
- A checkout of Cloud in a Bottle, for the playbooks: `git clone https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git ~/openhost`
- An SSH keypair. Make one if you do not have one: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""`

## Settings

Set these once in your shell. Every command below reads them, so this is the only place you fill in values.

```bash
# --- how to reach the VM ---
export VM_HOST=127.0.0.1               # or the VM's IP, if it has one of its own
export SSH_PORT=2222                   # 22 if you're connecting to the VM directly
export HTTP_PORT=8080                  # port on your machine that reaches VM :8080
export VM_USER=ubuntu                  # a sudo-capable login in the VM
export SSH_KEY=~/.ssh/id_ed25519       # your SSH private key ($SSH_KEY.pub must exist)

# --- Cloud in a Bottle ---
export DOMAIN=lvh.me:8080              # zone domain for app routing (see step 2.2)
export OPENHOST_REPO=~/openhost        # your checkout of the repo
```

## Part 1: build an Ubuntu VM with QEMU

Skip this if you already have a VM meeting the requirements above.

### QEMU settings

The defaults target an Apple Silicon (arm64) Mac. See [other hosts](#other-hosts-x86_64-linux) for x86_64 and Linux substitutions.

```bash
export VM_DIR=~/openhost-vm            # holds the disk, firmware vars, seed
export DISK_SIZE=40G
export RAM_MB=8192
export CPUS=4

export UBUNTU_IMG_URL="https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img"
export QEMU=qemu-system-aarch64
export ACCEL=hvf                       # macOS accelerator; Linux: kvm
export EFI_CODE=/opt/homebrew/share/qemu/edk2-aarch64-code.fd
export EFI_VARS_TEMPLATE=/opt/homebrew/share/qemu/edk2-arm-vars.fd
```

Install QEMU with `brew install qemu` on macOS, or `sudo apt install qemu-system-arm qemu-system-x86 qemu-utils` on Debian/Ubuntu.

### 1.1 Create the disk

Ubuntu's cloud image is a prebuilt qcow2 that configures itself from cloud-init on first boot, so there is no interactive installer. Download it as the VM's disk and grow it:

```bash
mkdir -p "$VM_DIR"
curl -L -o "$VM_DIR/disk.qcow2" "$UBUNTU_IMG_URL"
qemu-img resize "$VM_DIR/disk.qcow2" "$DISK_SIZE"   # cloud-init grows the rootfs to fill it
cp "$EFI_VARS_TEMPLATE" "$VM_DIR/efi-vars.fd"       # writable UEFI variable store
```

### 1.2 Build the cloud-init seed

Cloud-init reads a `user-data` and `meta-data` pair off a small ISO labelled `CIDATA`. This one creates `$VM_USER`, imports your SSH key, and sets a sudo password. The password defaults to `ubuntu` and you hand it to Ansible later. Change it here if you like.

```bash
cat > "$VM_DIR/user-data" <<EOF
#cloud-config
users:
  - name: $VM_USER
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) ALL
    lock_passwd: false
    ssh_authorized_keys:
      - $(cat "$SSH_KEY.pub")
chpasswd:
  expire: false
  users:
    - {name: $VM_USER, password: ubuntu, type: text}
ssh_pwauth: false
EOF
: > "$VM_DIR/meta-data"        # must exist, but stays empty

# Pack them into a CIDATA seed ISO.
# macOS:
hdiutil makehybrid -iso -joliet -default-volume-name CIDATA \
  -o "$VM_DIR/seed.iso" "$VM_DIR/user-data" "$VM_DIR/meta-data"
# Linux (needs cloud-image-utils):
# cloud-localds "$VM_DIR/seed.iso" "$VM_DIR/user-data" "$VM_DIR/meta-data"
```

### 1.3 Boot it

Boot headless, with the seed attached and two ports forwarded in from your machine: SSH, and the dashboard. Leave this running in its own terminal, or a `tmux` window, and use a second terminal for Part 2. First boot takes a minute while cloud-init runs.

```bash
"$QEMU" \
  -machine virt,accel=$ACCEL -cpu host -smp $CPUS -m $RAM_MB \
  -drive if=pflash,format=raw,readonly=on,file="$EFI_CODE" \
  -drive if=pflash,format=raw,file="$VM_DIR/efi-vars.fd" \
  -device virtio-rng-pci \
  -netdev user,id=n0,hostfwd=tcp::$SSH_PORT-:22,hostfwd=tcp::$HTTP_PORT-:8080 \
  -device virtio-net-pci,netdev=n0 \
  -drive if=none,file="$VM_DIR/disk.qcow2",format=qcow2,id=hd0 -device virtio-blk-pci,drive=hd0 \
  -drive if=none,file="$VM_DIR/seed.iso",format=raw,readonly=on,id=cd0 -device virtio-blk-pci,drive=cd0 \
  -nographic
```

This is also the restart command later. The seed ISO is harmless to leave attached, since cloud-init only applies it once per VM.

`-nographic` wires the VM's serial console to this terminal. Quit QEMU with `Ctrl-A` then `X`, not `Ctrl-C`.

Confirm SSH works from your second terminal. Accept the host key, and retry for a few seconds if cloud-init is still finishing.

```bash
ssh -p "$SSH_PORT" "$VM_USER@$VM_HOST" 'lsb_release -ds && echo SSH_OK'
```

### Other hosts (x86_64, Linux)

The settings above assume an arm64 Mac. On an x86_64 host:

- `QEMU=qemu-system-x86_64`, and swap `-machine virt` for `-machine q35`.
- `ACCEL=kvm` on Linux, `ACCEL=hvf` on an Intel Mac.
- Use OVMF firmware instead of arm EDK2: `EFI_CODE=/usr/share/OVMF/OVMF_CODE.fd` and `EFI_VARS_TEMPLATE=/usr/share/OVMF/OVMF_VARS.fd`. On Debian/Ubuntu, install `ovmf`.
- Use the amd64 cloud image, and build the seed with `cloud-localds`.

On arm64 Linux keep `-machine virt`, but use `EFI_CODE=/usr/share/AAVMF/AAVMF_CODE.fd` and the matching `AAVMF_VARS.fd`.

## Part 2: install Cloud in a Bottle into the VM

Run these from your machine, not inside the VM, with the VM running.

### 2.1 Run the playbook

```bash
cd "$OPENHOST_REPO"

ANSIBLE_HOST_KEY_CHECKING=False \
ansible-playbook ansible/setup.yml \
  -i "$VM_HOST," \
  -e ansible_connection=ssh \
  -e ansible_port=$SSH_PORT \
  -e initial_user=$VM_USER \
  -e domain=$DOMAIN \
  -e local_http_only=true \
  -e bind_host=0.0.0.0 \
  -e public_ip=127.0.0.1 \
  -e skip_apt_upgrade=true \
  --private-key=$SSH_KEY \
  --ask-become-pass          # the VM user's sudo password
```

This installs rootless Podman and pixi inside the VM, clones Cloud in a Bottle from GitHub, writes its config, and starts it as a systemd service. It takes several minutes the first time.

Two flags matter. `local_http_only=true` puts the instance in HTTP-only mode: no TLS, no CoreDNS, no Caddy, just the router serving plain HTTP on port 8080. `bind_host=0.0.0.0` makes it listen on the VM's network interface instead of only loopback, which is what a NAT port forward connects to. If you are reaching the VM over an SSH tunnel instead, you can leave `bind_host` at its `127.0.0.1` default.

### 2.2 Why `DOMAIN=lvh.me:8080`

Apps are routed by subdomain (`<app>.<domain>`), so the zone domain has to be something whose subdomains resolve. `localhost` does not qualify. `lvh.me` and `*.lvh.me` both resolve to `127.0.0.1` in public DNS, so `http://myapp.lvh.me:8080` reaches the router with nothing to configure. Any other wildcard-to-loopback domain works too, including one you run yourself.

Include the `:8080`. The router builds absolute login and redirect URLs from this value, and without the port they point at `:80` and dead-end.

### 2.3 Claim it

The playbook prints a claim URL at the end:

```
Claim URL: http://lvh.me:8080/setup?claim=<token>
```

Open it and set the owner username and password. Before it is claimed, every request redirects to a gated `/setup`.

If you lose the token, re-run the playbook, which is idempotent, or read it out of the VM:

```bash
ssh -p $SSH_PORT $VM_USER@$VM_HOST \
  'sudo cat /home/host/.openhost/local_compute_space/first_boot.toml'
```

## Verify and manage

```bash
curl http://localhost:$HTTP_PORT/health         # -> {"status":"ok"}

ssh -p $SSH_PORT $VM_USER@$VM_HOST 'sudo systemctl status openhost'
ssh -p $SSH_PORT $VM_USER@$VM_HOST 'sudo journalctl -u openhost -f'
```

- Stop the VM: shut it down however your VM host does it, or `ssh -p $SSH_PORT $VM_USER@$VM_HOST sudo poweroff`. Under QEMU you can also press `Ctrl-A` then `X` in its terminal.
- Restart the VM: Cloud in a Bottle comes back up on its own. Under QEMU, re-run the boot command from [step 1.3](#13-boot-it).
- Throw it away: delete the VM. Under QEMU that is `rm -rf "$VM_DIR"`, and nothing was installed on your machine except QEMU itself.
- Upgrade Cloud in a Bottle: use the update button on the dashboard's settings page.

The systemd service, logs and diagnostics are in [Debugging](../operation/debugging.md).

## Going public

An instance in a VM is a real deployment, not just a test rig. Serving it on the internet takes three things beyond the walkthrough above.

**Networking.** Delegate a DNS zone to your public IPv4 and get ports 53, 80, and 443 through to the VM. On a home connection that means dynamic DNS plus forwarding rules on both your router and the VM host. See [Exposing a home server](./home_network.md).

**An ACME account key**, so the instance can get its own certificates. Generate one on your machine:

```bash
cd "$OPENHOST_REPO"
pixi run python scripts/generate_acme_key.py ansible/secrets/certbot_private_key.json --email you@example.com
```

**A playbook run with TLS on.** Use your real zone domain, pass your public IPv4, and drop `local_http_only` and `bind_host`. Caddy terminates TLS on port 443 and proxies to the router over loopback, so the router does not need to listen on the VM's interface anymore.

```bash
ansible-playbook ansible/setup.yml \
  -i "$VM_HOST," \
  -e ansible_connection=ssh \
  -e ansible_port=$SSH_PORT \
  -e initial_user=$VM_USER \
  -e domain=mycooldomain.com \
  -e public_ip=<your public IPv4> \
  -e acme_directory_url=https://acme-v02.api.letsencrypt.org/directory \
  --private-key=$SSH_KEY \
  --ask-become-pass
```

Set `acme_directory_url` to Let's Encrypt, which is what the key you just generated is registered with. The built-in default is Google Trust Services, which needs an account binding you have to request separately.

The instance gets a wildcard certificate covering `mycooldomain.com` and `*.mycooldomain.com` over DNS-01, and comes up at `https://mycooldomain.com/`.

Easiest is to do this from the start, on a VM you have not claimed yet. Converting an instance you already claimed at `lvh.me` is more work: the playbook preserves an existing config, so you have to re-run it with `-e overwrite_existing=true` to switch the instance out of HTTP-only mode, and the primary domain is fixed in the database at claim time, so you add the public domain from the dashboard's domain settings rather than replacing it.
