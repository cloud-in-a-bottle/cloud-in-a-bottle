# Running OpenHost in a local QEMU VM

> The authoritative deployment reference is
> **[`ansible/readme.md`](https://github.com/imbue-openhost/openhost/blob/main/ansible/readme.md)**
> and `ansible/setup.yml`; this guide walks the local-VM path end to end.
>
> Upstream docs for the tools used here:
> [QEMU](https://www.qemu.org/download/) ·
> [Ubuntu Server](https://ubuntu.com/download/server)

This gets OpenHost running on an **Ubuntu 24.04 VM under QEMU** on your
desktop — good for trying it out before you buy a dedicated domain or machine.
Two parts:

1. **[Build the VM](#part-1--build-an-ubuntu-vm-in-qemu)** — install QEMU and
   stand up an Ubuntu VM.
2. **[Deploy OpenHost](#part-2--deploy-openhost-onto-the-vm)** — run the Ansible
   playbook against that VM (HTTP-only mode; no domain needed).

For a dedicated instance instead, see
[Going to production](#going-to-production-real-host--domain).

---

## Settings — edit these once

Set these in your shell; the commands below use them, so you only fill in
values here. The defaults target an **Apple Silicon (arm64) Mac**; see
[Other hosts](#other-hosts-x86_64--linux) for x86_64 / Linux substitutions.

```bash
# --- where the VM lives + how to reach it ---
export VM_DIR=~/openhost-vm            # holds the disk, firmware vars, seed
export SSH_KEY=~/.ssh/id_ed25519       # your SSH private key ($SSH_KEY.pub must exist)
export VM_USER=ubuntu                  # the login cloud-init sets up in the VM
export SSH_PORT=2222                   # host port -> VM :22
export HTTP_PORT=8080                  # host port -> VM :8080 (the dashboard)

# --- VM size ---
export DISK_SIZE=40G
export RAM_MB=8192
export CPUS=4

# --- OpenHost ---
export DOMAIN=lvh.me:8080              # zone domain for app routing (see note in Part 2)
export OPENHOST_REPO=~/openhost        # path to your checkout of this repo

# --- QEMU (Apple Silicon / arm64 defaults) ---
# Ubuntu cloud image for your host's architecture (swap the -arm64 suffix for
# -amd64 on x86_64):
export UBUNTU_IMG_URL="https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img"
export QEMU=qemu-system-aarch64
export ACCEL=hvf                       # macOS accelerator; Linux: kvm
export EFI_CODE=/opt/homebrew/share/qemu/edk2-aarch64-code.fd
export EFI_VARS_TEMPLATE=/opt/homebrew/share/qemu/edk2-arm-vars.fd

# If you don't already have an SSH key:  ssh-keygen -t ed25519 -f "$SSH_KEY" -N ""
```

---

## Part 1 — Build an Ubuntu VM in QEMU

### 1. Install QEMU

```bash
# macOS
brew install qemu

# Debian/Ubuntu Linux
sudo apt install qemu-system-arm qemu-system-x86 qemu-utils
```

Verify: `"$QEMU" --version`. (Authoritative install docs:
<https://www.qemu.org/download/>.)

### 2. Get the Ubuntu cloud image + create the disk

The cloud image is a prebuilt qcow2 that configures itself from cloud-init on
first boot — no interactive installer. Download it as the VM's disk and grow it
to `$DISK_SIZE`:

```bash
mkdir -p "$VM_DIR"                              # holds the disk, firmware vars, seed
curl -L -o "$VM_DIR/disk.qcow2" "$UBUNTU_IMG_URL"
qemu-img resize "$VM_DIR/disk.qcow2" "$DISK_SIZE"   # cloud-init grows the rootfs to fill it
cp "$EFI_VARS_TEMPLATE" "$VM_DIR/efi-vars.fd"   # writable UEFI variable store
```

### 3. Build the cloud-init seed

Cloud-init reads a `user-data` + `meta-data` pair from a small ISO labelled
`CIDATA`. This one creates `$VM_USER`, imports your SSH key, and sets a sudo
password (`ubuntu` by default — you'll hand it to Ansible's `--ask-become-pass`;
change it here if you like).

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
: > "$VM_DIR/meta-data"        # empty file is required but has no content

# Pack them into a CIDATA seed ISO:
# macOS:
hdiutil makehybrid -iso -joliet -default-volume-name CIDATA \
  -o "$VM_DIR/seed.iso" "$VM_DIR/user-data" "$VM_DIR/meta-data"
# Linux (cloud-image-utils):  cloud-localds "$VM_DIR/seed.iso" "$VM_DIR/user-data" "$VM_DIR/meta-data"
```

### 4. Boot the VM (headless)

Boot the disk with the seed attached, headless (serial on your terminal). Leave
this running in its own terminal (or a `tmux`/`screen` session) and use a new
terminal for Part 2. First boot takes a moment while cloud-init runs.

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

Confirm SSH works from another terminal (accept the host key on first connect;
retry for a few seconds if cloud-init is still finishing):

```bash
ssh -p "$SSH_PORT" "$VM_USER@localhost" 'lsb_release -ds && echo SSH_OK'
```

> The seed ISO is harmless to leave attached — cloud-init only applies it once
> per VM, so this same command is also your restart command.
>
> `-nographic` wires the VM's serial console to this terminal; quit QEMU with
> `Ctrl-A` then `X`. (Don't `Ctrl-C`.)

### Manual install from the Server ISO (TODO)

> **TODO:** document the alternative interactive install from the Ubuntu Server
> ISO (boot subiquity with a display, use the whole disk, install OpenSSH, import
> your SSH key) for anyone who'd rather not use the cloud image. To be filled in.

### Other hosts (x86_64 / Linux)

The commands above assume an arm64 Mac. On an **x86_64** host, change the
Settings block:

- `QEMU=qemu-system-x86_64`, and swap `-machine virt` for `-machine q35`.
- `ACCEL=kvm` on Linux (`ACCEL=hvf` on an Intel Mac).
- Firmware: OVMF instead of arm EDK2 —
  `EFI_CODE=/usr/share/OVMF/OVMF_CODE.fd`,
  `EFI_VARS_TEMPLATE=/usr/share/OVMF/OVMF_VARS.fd` (install `ovmf` on Debian/Ubuntu).
- Use the **amd64** cloud image (`UBUNTU_IMG_URL` above), and build the seed with
  `cloud-localds` instead of `hdiutil`.

On arm64 Linux, keep `-machine virt` but use
`EFI_CODE=/usr/share/AAVMF/AAVMF_CODE.fd` and the matching `AAVMF_VARS.fd`.

---

## Part 2 — Deploy OpenHost onto the VM

Run these from your **desktop** (not inside the VM), with the VM from Part 1
still running.

### 1. One-time prerequisites

```bash
# Ansible on your desktop (control machine)
uv tool install ansible-core        # or: pipx install ansible-core

# A checkout of this repo (skip if you already have $OPENHOST_REPO)
git clone https://github.com/imbue-openhost/openhost.git "$OPENHOST_REPO"
```

### 2. Run the playbook (HTTP-only)

HTTP-only mode skips TLS, CoreDNS, and Caddy — the router serves plain HTTP on
`:8080`, reachable via the port forward you set up.

```bash
cd "$OPENHOST_REPO"

# Known rough edge: in HTTP-only mode the playbook still copies an ACME key it
# never uses. A placeholder satisfies it (git-ignored; harmless without TLS).
echo '{}' > ansible/secrets/certbot_private_key.json

ANSIBLE_HOST_KEY_CHECKING=False \
ansible-playbook ansible/setup.yml \
  -i '127.0.0.1,' \
  -e ansible_connection=ssh \
  -e ansible_port=$SSH_PORT \
  -e initial_user=$VM_USER \
  -e domain=$DOMAIN \
  -e local_http_only=true \
  -e bind_host=0.0.0.0 \
  -e public_ip=127.0.0.1 \
  -e skip_apt_upgrade=true \
  --private-key=$SSH_KEY \
  --ask-become-pass          # the VM user's sudo password (set in the cloud-init seed)
```

This installs rootless Podman, pixi, the systemd units, and deploys OpenHost's
default apps. It takes several minutes the first time.

> **Why `DOMAIN=lvh.me:8080`?** OpenHost routes apps by subdomain
> (`<app>.<domain>`), and `localhost` can't have working wildcard subdomains.
> `lvh.me` and `*.lvh.me` resolve to `127.0.0.1` in public DNS with no setup, so
> `http://myapp.lvh.me:8080` just works. Include the **`:8080`** so the router's
> absolute login/redirect URLs keep the port (otherwise they point at `:80` and
> dead-end).

### 3. Claim it

The playbook prints a **claim URL** at the end:

```
http://lvh.me:8080/setup?claim=<token>
```

Open it (or `http://localhost:$HTTP_PORT/setup?claim=<token>`), set the owner
username + password, and you're in. Visiting the site before claiming just
redirects to a gated `/setup`. Lost the token? Re-run the playbook (idempotent)
for a fresh URL, or pass your own with `-e claim_token=<secret>`.

---

## Verify & manage

```bash
# From the desktop, through the port forward:
curl http://localhost:$HTTP_PORT/health            # -> {"status":"ok"}

# Service status / logs (SSH into the VM):
ssh -p $SSH_PORT $VM_USER@localhost 'sudo systemctl status openhost'
ssh -p $SSH_PORT $VM_USER@localhost 'sudo journalctl -u openhost -f'
```

- **Stop the VM:** in its terminal, `Ctrl-A` then `X` (or `ssh … sudo poweroff`).
- **Restart the VM:** re-run the Part 1 step 4 boot command.
- **Re-deploy after code changes:** `ansible-playbook ansible/deploy.yml …`
  with the same `-i`/`-e` flags (see `ansible/readme.md`).

---

## Going to production (real host + domain)

The VM flow above is for local use. For a public instance, the only things that
change are the *host* (any Ubuntu 24.04 server — cloud VPS or bare metal, reached
over SSH as `root`) and turning on TLS by dropping `local_http_only`:

1. **DNS** — delegate your zone to the server so its built-in CoreDNS can answer
   ACME DNS-01 and serve `*.<zone>`:

   | Record | Name | Value |
   |--------|------|-------|
   | `A`    | `ns1.host.example.com` | `<SERVER_IP>` |
   | `NS`   | `host.example.com`     | `ns1.host.example.com` |

2. **ACME key** — `python scripts/generate_acme_key.py ansible/secrets/certbot_private_key.json --email you@example.com`
   (or use the `cert_api` broker; see `ansible/readme.md`).

3. **Deploy** (TLS is the default — no `local_http_only`):

   ```bash
   ansible-playbook ansible/setup.yml -i <SERVER_IP>, \
     -e initial_user=root -e domain=host.example.com \
     --private-key=~/.ssh/your_key
   ```

Your instance comes up at `https://host.example.com/`. Full options and the
authoritative reference are in
**[`ansible/readme.md`](https://github.com/imbue-openhost/openhost/blob/main/ansible/readme.md)**.
