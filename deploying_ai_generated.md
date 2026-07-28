# Running OpenHost in a local QEMU VM

> **AI-generated.** Per this repo's convention, AI-written docs live in
> `*_ai_generated.md` and are not a hand-maintained source of truth. The
> authoritative deployment reference is **[`ansible/readme.md`](ansible/readme.md)**
> and `ansible/setup.yml`; this file just walks the local-VM path end to end.
>
> Upstream docs for the tools used here:
> [QEMU](https://www.qemu.org/download/) ·
> [Ubuntu Server](https://ubuntu.com/download/server) ·
> [Ubuntu autoinstall](https://ubuntu.com/server/docs/install/autoinstall)

This gets OpenHost running on a throwaway **Ubuntu 24.04 VM under QEMU** on your
desktop — good for trying it out or developing against it before you point a
real domain at a real server. Two parts:

1. **[Build the VM](#part-1--build-an-ubuntu-vm-in-qemu)** — install QEMU and
   stand up an Ubuntu VM.
2. **[Deploy OpenHost](#part-2--deploy-openhost-onto-the-vm)** — run the Ansible
   playbook against that VM (HTTP-only mode; no domain needed).

For a real public instance instead, see
[Going to production](#going-to-production-real-host--domain).

---

## Settings — edit these once

Set these in your shell; every command below uses them, so you only fill in
values here. The defaults target an **Apple Silicon (arm64) Mac**; see
[Other hosts](#other-hosts-x86_64--linux) for x86_64 / Linux substitutions.

```bash
# --- where the VM lives + how to reach it ---
export VM_DIR=~/openhost-vm            # holds the disk, firmware vars, ISO
export SSH_KEY=~/.ssh/id_ed25519       # your SSH private key ($SSH_KEY.pub must exist)
export VM_USER=ubuntu                  # the login you'll create in the installer
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
export UBUNTU_ISO_URL="https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-live-server-arm64.iso"
export QEMU=qemu-system-aarch64
export ACCEL=hvf                       # macOS accelerator; Linux: kvm
export EFI_CODE=/opt/homebrew/share/qemu/edk2-aarch64-code.fd
export EFI_VARS_TEMPLATE=/opt/homebrew/share/qemu/edk2-arm-vars.fd

mkdir -p "$VM_DIR"
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

### 2. Get the Ubuntu Server ISO + create the disk

```bash
curl -L -o "$VM_DIR/ubuntu.iso" "$UBUNTU_ISO_URL"
qemu-img create -f qcow2 "$VM_DIR/disk.qcow2" "$DISK_SIZE"
cp "$EFI_VARS_TEMPLATE" "$VM_DIR/efi-vars.fd"   # writable UEFI variable store
```

Use the **arm64** ISO on Apple Silicon, the **amd64** ISO on an x86_64 host
(pick from <https://ubuntu.com/download/server>).

### 3. Install Ubuntu (interactive)

Boot the installer with a display. This opens a QEMU window; the port forwards
are set up now so the *installed* system is reachable later.

```bash
"$QEMU" \
  -machine virt,accel=$ACCEL -cpu host -smp $CPUS -m $RAM_MB \
  -drive if=pflash,format=raw,readonly=on,file="$EFI_CODE" \
  -drive if=pflash,format=raw,file="$VM_DIR/efi-vars.fd" \
  -device virtio-gpu-pci -display default,show-cursor=on \
  -device qemu-xhci -device usb-kbd -device usb-tablet \
  -device virtio-rng-pci \
  -netdev user,id=n0,hostfwd=tcp::$SSH_PORT-:22,hostfwd=tcp::$HTTP_PORT-:8080 \
  -device virtio-net-pci,netdev=n0 \
  -drive if=none,file="$VM_DIR/disk.qcow2",format=qcow2,id=hd0 -device virtio-blk-pci,drive=hd0 \
  -drive if=none,file="$VM_DIR/ubuntu.iso",format=raw,readonly=on,id=cd0 -device virtio-blk-pci,drive=cd0
```

In the Ubuntu Server installer:

- Accept the defaults for language/keyboard; **network** works out of the box
  (QEMU user-mode NAT gives the VM internet).
- **Storage:** use the entire disk.
- **Profile:** set your name and a username — use the same value as `$VM_USER`
  (`ubuntu`), and pick a password (you'll hand it to Ansible's `--ask-become-pass`).
- **✅ Install OpenSSH server**, and **import your SSH key** — paste the contents
  of `$SSH_KEY.pub`, or import from GitHub if your key is on your account.

When it finishes, choose **Reboot**. Once it powers down, close the QEMU window.

> Prefer a fully unattended, repeatable install? Ubuntu's
> [autoinstall / cloud-init](https://ubuntu.com/server/docs/install/autoinstall)
> can script all of the above via a NoCloud seed ISO.

### 4. Boot the installed VM

Same command **without** the ISO, headless (serial on your terminal). Leave this
running in its own terminal (or a `tmux`/`screen` session) and use a new
terminal for Part 2.

```bash
"$QEMU" \
  -machine virt,accel=$ACCEL -cpu host -smp $CPUS -m $RAM_MB \
  -drive if=pflash,format=raw,readonly=on,file="$EFI_CODE" \
  -drive if=pflash,format=raw,file="$VM_DIR/efi-vars.fd" \
  -device virtio-rng-pci \
  -netdev user,id=n0,hostfwd=tcp::$SSH_PORT-:22,hostfwd=tcp::$HTTP_PORT-:8080 \
  -device virtio-net-pci,netdev=n0 \
  -drive if=none,file="$VM_DIR/disk.qcow2",format=qcow2,id=hd0 -device virtio-blk-pci,drive=hd0 \
  -nographic
```

Confirm SSH works from another terminal (accept the host key on first connect):

```bash
ssh -p "$SSH_PORT" "$VM_USER@localhost" 'lsb_release -ds && echo SSH_OK'
```

> `-nographic` wires the VM's serial console to this terminal; quit QEMU with
> `Ctrl-A` then `X`. (Don't `Ctrl-C`.)

### Other hosts (x86_64 / Linux)

The commands above assume an arm64 Mac. On an **x86_64** host, change the
Settings block:

- `QEMU=qemu-system-x86_64`, and swap `-machine virt` for `-machine q35`.
- `ACCEL=kvm` on Linux (`ACCEL=hvf` on an Intel Mac).
- Firmware: OVMF instead of arm EDK2 —
  `EFI_CODE=/usr/share/OVMF/OVMF_CODE.fd`,
  `EFI_VARS_TEMPLATE=/usr/share/OVMF/OVMF_VARS.fd` (install `ovmf` on Debian/Ubuntu).
- Use the **amd64** Ubuntu ISO.

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
  --ask-become-pass          # enter the VM user's sudo password from the installer
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
authoritative reference are in **[`ansible/readme.md`](ansible/readme.md)**.
