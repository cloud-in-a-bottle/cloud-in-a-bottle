#!/usr/bin/env bash
# build.sh: build a bootable Cloud in a Bottle VM image from the Ubuntu 24.04 cloud image.
#
# The recipe is deliberately plain: take Ubuntu's official cloud-image qcow2,
# boot it once under QEMU with a cloud-init seed that runs our existing,
# tested provision.sh, let it power itself off, then freeze the resulting disk.
# No Packer, no autoinstall ISO dance — just the exact code path a real deploy
# uses. Output is a QEMU qcow2 and (optionally) a VirtualBox OVA.
#
# Builds an image for the host's arch: amd64, or arm64 (which boots via UEFI
# and has no OVA).
#
# The image comes up out of the box in HTTP-only mode bound to 0.0.0.0, so the
# dashboard is reachable at http://<vm-ip>:8080 with a default console password.
# No domain, DNS, or TLS setup required to try it. Claiming is open by default
# (no token) since the image is private behind NAT and a shipped default token
# would be a public non-secret; pass --claim-token to bake one instead.
#
# Pass --public (with --public-ip) to bake a TLS image instead: it provisions
# with CoreDNS + Caddy + Let's Encrypt for --domain, ready to serve publicly
# once you delegate DNS and open ports 53/80/443. Claiming is token-gated in
# this mode (open claiming is refused on a reachable instance).
#
# Pass --raspi (on an arm64 host) to build a Raspberry Pi SD-card image
# instead: the same recipe, starting from Ubuntu's preinstalled Raspberry Pi
# image and booting its own kernel on a generic QEMU machine (the Pi kernel runs
# there fine; only the Pi's FAT boot partition is Pi-specific, and the build
# leaves it bootable). It provisions in LAN mode (a .local domain, published
# over mDNS, plain http), and on first boot the instance takes its address from
# the hostname: http://<hostname>.local. Write the .img.xz with Raspberry Pi
# Imager, whose OS customisation (hostname, wifi, SSH key) the image honors.
#
# Usage (run on a Linux host with KVM, or an Apple silicon Mac for arm64):
#   image/build.sh [options]
#
# Options:
#   --branch <branch>     Git branch of Cloud in a Bottle app code to clone (default: main)
#   --repo <url>          Git repo URL to clone app code from
#                         (default: cloud-in-a-bottle/cloud-in-a-bottle)
#   --provision-script <path>
#                         provision.sh to embed and run in the build VM
#                         (default: this repo's scripts/provision.sh). Embedded
#                         from the working tree, so the branch need not be pushed.
#   --domain <domain>     App subdomain-routing domain baked in (default: lvh.me,
#                         or bottle.local with --raspi). With --public this is
#                         the real domain served over TLS.
#   --public              Build a TLS image (CoreDNS + Caddy + Let's Encrypt for
#                         --domain) instead of the default HTTP-only image.
#                         Requires --public-ip.
#   --public-ip <ip>      Public IPv4 baked into the config for DNS records
#                         (required with --public).
#   --acme-key <path>     Pre-registered ACME account key to bake in (--public).
#                         Default: the build generates and registers one.
#   --acme-email <email>  Email for the generated ACME account (--public, when
#                         no --acme-key is given).
#   --claim-token <tok>   Bake in a claim token gating /setup. Default: none —
#                         claiming is open (claim_token_required=false), since
#                         the image is private behind NAT. Set this to require a
#                         token (e.g. for a customized image you distribute).
#   --password <pw>       Default console password for the `host` user
#                         (default: cloudinabottle; with --raspi, none: the
#                         account is locked, and console/SSH access comes from
#                         Raspberry Pi Imager's settings or the stock `ubuntu`
#                         console login)
#   --ssh-pubkey <path>   Optional SSH public key file to authorize for `host`
#                         (SSH is key-only; without this, access is console-only)
#   --version <v>         Version string used in artifact filenames
#                         (default: `git describe` or "dev")
#   --disk-size <size>    Virtual disk size baked into the image — the default
#                         floor only (default: 20G; 14G with --raspi, which
#                         fits a "16GB" SD card). The image grows its root
#                         filesystem to fill whatever disk it is installed onto
#                         on first boot, so users pick the real size by sizing
#                         the VM disk (or the physical disk on bare metal).
#   --swap-size <gib>     Swap file size in GiB baked into the image (default: 4;
#                         2 with --raspi).
#                         Tweak after install by SSHing in and resizing /swapfile
#                         (or from the dashboard settings page).
#   --mem <mb>            Build VM memory in MB (default: 4096)
#   --cpus <n>            Build VM vCPUs (default: 2)
#   --output-dir <dir>    Where artifacts land (default: image/out)
#   --no-ova              Skip the VirtualBox OVA; produce only the qcow2
#                         (always skipped for arm64)
#   --raspi               Build a Raspberry Pi SD-card image (.img.xz, plus a
#                         Raspberry Pi Imager repository entry) instead of VM
#                         images. arm64 hosts only.
#   --timeout <sec>       Max seconds to wait for the build boot (default: 1800)
#   -h, --help            Show this help
#
# Requirements: qemu-system-x86_64 (amd64) or qemu-system-aarch64 plus UEFI
# firmware from qemu-efi-aarch64 (arm64), qemu-img, cloud-localds
# (cloud-image-utils) or xorriso/genisoimage/mkisofs, curl, tar, timeout, and
# KVM (/dev/kvm) or HVF on macOS. --raspi needs an arm64 Linux host with KVM,
# plus mtools, sfdisk (fdisk), xz and sha256sum.

set -euo pipefail

# ---- Defaults ----
case "$(uname -m)" in
    aarch64|arm64) ARCH="arm64" ;;
    *)             ARCH="amd64" ;;
esac
BRANCH="main"
REPO_URL="https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git"
DOMAIN=""         # default depends on --raspi; resolved after parsing
CLAIM_TOKEN=""   # empty => open claim (no token required); set to bake a token
HOST_PASSWORD=""  # default depends on --raspi; resolved after parsing
HOST_PASSWORD_SET="false"
SSH_PUBKEY_FILE=""
VERSION=""
DISK_SIZE=""
SWAP_SIZE_GB=""
MEM_MB="4096"
CPUS="2"
BUILD_TIMEOUT="1800"
MAKE_OVA="true"
PUBLIC="false"
PUBLIC_IP=""
ACME_KEY_FILE=""
ACME_EMAIL=""
RASPI="false"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/out"
CACHE_DIR="$SCRIPT_DIR/cache"
PROVISION_SCRIPT="$SCRIPT_DIR/../scripts/provision.sh"

# Print the leading comment block (everything from line 2 up to the first
# non-comment line), stripped of the leading "# ".
usage() { sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "${BASH_SOURCE[0]}"; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --branch)       BRANCH="$2"; shift 2 ;;
        --repo)         REPO_URL="$2"; shift 2 ;;
        --provision-script) PROVISION_SCRIPT="$2"; shift 2 ;;
        --domain)       DOMAIN="$2"; shift 2 ;;
        --claim-token)  CLAIM_TOKEN="$2"; shift 2 ;;
        --password)     HOST_PASSWORD="$2"; HOST_PASSWORD_SET="true"; shift 2 ;;
        --ssh-pubkey)   SSH_PUBKEY_FILE="$2"; shift 2 ;;
        --version)      VERSION="$2"; shift 2 ;;
        --disk-size)    DISK_SIZE="$2"; shift 2 ;;
        --swap-size)    SWAP_SIZE_GB="$2"; shift 2 ;;
        --mem)          MEM_MB="$2"; shift 2 ;;
        --cpus)         CPUS="$2"; shift 2 ;;
        --output-dir)   OUTPUT_DIR="$2"; shift 2 ;;
        --no-ova)       MAKE_OVA="false"; shift ;;
        --timeout)      BUILD_TIMEOUT="$2"; shift 2 ;;
        --public)       PUBLIC="true"; shift ;;
        --public-ip)    PUBLIC_IP="$2"; shift 2 ;;
        --acme-key)     ACME_KEY_FILE="$2"; shift 2 ;;
        --acme-email)   ACME_EMAIL="$2"; shift 2 ;;
        --raspi)        RASPI="true"; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

# ---- Defaults that depend on --raspi ----
if [ "$RASPI" = "true" ]; then
    if [ "$ARCH" != "arm64" ]; then
        echo "Error: --raspi needs an arm64 host (it boots the Pi image's arm64 kernel under KVM/HVF)." >&2
        exit 1
    fi
    if [ "$PUBLIC" = "true" ]; then
        echo "Error: --raspi and --public are exclusive (the Pi image is LAN-only)." >&2
        exit 1
    fi
    DOMAIN="${DOMAIN:-bottle.local}"
    case "${DOMAIN%%:*}" in
        *.local) ;;
        *) echo "Error: --raspi needs a .local --domain (got $DOMAIN)." >&2; exit 1 ;;
    esac
    [ "$HOST_PASSWORD_SET" = "true" ] || HOST_PASSWORD=""
    DISK_SIZE="${DISK_SIZE:-14G}"
    SWAP_SIZE_GB="${SWAP_SIZE_GB:-2}"
    MAKE_OVA="false"
else
    DOMAIN="${DOMAIN:-lvh.me}"
    [ "$HOST_PASSWORD_SET" = "true" ] || HOST_PASSWORD="cloudinabottle"
    DISK_SIZE="${DISK_SIZE:-20G}"
    SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"
fi

# ---- Per-arch settings ----
case "$ARCH" in
    amd64)
        QEMU="qemu-system-x86_64"
        QEMU_MACHINE=()
        ;;
    arm64)
        QEMU="qemu-system-aarch64"
        # Ubuntu's arm64 cloud image boots only via UEFI, so -bios points at the
        # edk2 firmware. That is all the build boot needs; the image itself
        # boots through the removable-media fallback path (EFI/BOOT/BOOTAA64.EFI)
        # on any UEFI arm64 VM, so no NVRAM state needs to ship with it.
        # Debian/Ubuntu's qemu-efi-aarch64 puts the firmware here. Elsewhere
        # (e.g. Homebrew) it's in QEMU's data dir, where -bios finds it by name.
        UEFI_FW=/usr/share/qemu-efi-aarch64/QEMU_EFI.fd
        [ -f "$UEFI_FW" ] || UEFI_FW=edk2-aarch64-code.fd
        QEMU_MACHINE=(-machine virt -bios "$UEFI_FW")
        # The Pi image has no UEFI bootloader; build.sh boots its kernel directly.
        [ "$RASPI" = "true" ] && QEMU_MACHINE=(-machine virt)
        # The VirtualBox OVF below describes an x86 machine.
        MAKE_OVA="false"
        ;;
esac
CLOUD_IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-$ARCH.img"

# ---- Validate --public flags ----
if [ "$PUBLIC" = "true" ]; then
    if [ -z "$PUBLIC_IP" ]; then
        echo "Error: --public requires --public-ip <ip> (baked in for DNS records)." >&2
        exit 1
    fi
elif [ -n "$PUBLIC_IP" ] || [ -n "$ACME_KEY_FILE" ] || [ -n "$ACME_EMAIL" ]; then
    echo "Error: --public-ip / --acme-key / --acme-email require --public." >&2
    exit 1
fi
if [ -n "$ACME_KEY_FILE" ] && [ ! -f "$ACME_KEY_FILE" ]; then
    echo "Error: --acme-key file not found: $ACME_KEY_FILE" >&2
    exit 1
fi

# ---- Portable helpers ----
file_size() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1"; }

need() {
    command -v "$1" >/dev/null 2>&1 || { echo "Error: '$1' not found. $2" >&2; exit 1; }
}

# ---- Dependency checks ----
need qemu-img       "Install qemu-utils."
need "$QEMU"         "Install qemu-system-x86 (amd64) or qemu-system-arm (arm64)."

need curl           "Install curl."
need tar            "Install tar."
need timeout        "Install coreutils."
if [ "$RASPI" = "true" ]; then
    need mcopy      "Install mtools."
    need sfdisk     "Install fdisk."
    need xz         "Install xz-utils."
    need sha256sum  "Install coreutils."
fi

if [ -w /dev/kvm ]; then
    KVM_ARGS=(-enable-kvm -cpu host)
elif [ "$(sysctl -n kern.hv_support 2>/dev/null)" = "1" ]; then
    # macOS Hypervisor.framework.
    KVM_ARGS=(-accel hvf -cpu host)
else
    echo "Error: no writable /dev/kvm (or HVF on macOS)." >&2
    exit 1
fi

# Seed-ISO builder: prefer cloud-localds, fall back to xorriso/genisoimage/mkisofs.
# (--raspi needs none: its cloud-init reads the seed from the Pi's boot partition.)
SEED_TOOL=""
if [ "$RASPI" = "true" ]; then
    :
elif command -v cloud-localds >/dev/null 2>&1; then
    SEED_TOOL="cloud-localds"
elif command -v xorriso >/dev/null 2>&1; then
    SEED_TOOL="xorriso"
elif command -v genisoimage >/dev/null 2>&1; then
    SEED_TOOL="genisoimage"
elif command -v mkisofs >/dev/null 2>&1; then
    SEED_TOOL="mkisofs"
else
    echo "Error: need one of cloud-localds (cloud-image-utils), xorriso, genisoimage, or mkisofs." >&2
    exit 1
fi

if [ -z "$VERSION" ]; then
    VERSION="$(git -C "$SCRIPT_DIR" describe --tags --always --dirty 2>/dev/null || echo dev)"
fi

# Every artifact (qcow2, vmdk, ovf, ova) is this stem plus its extension, so the
# published download names are decided in exactly one place. .github/workflows/
# release.yml globs `cloud-in-a-bottle-*` to collect them, so keep the two in sync.
ARTIFACT_BASE="cloud-in-a-bottle-$VERSION-$ARCH"
[ "$RASPI" = "true" ] && ARTIFACT_BASE="cloud-in-a-bottle-$VERSION-raspi"

if [ ! -f "$PROVISION_SCRIPT" ]; then
    echo "Error: provision script not found: $PROVISION_SCRIPT" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR" "$CACHE_DIR"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "=== Cloud in a Bottle VM image build ==="
echo "  Version:      $VERSION"
echo "  Arch:         $ARCH"
echo "  Repo/branch:  $REPO_URL @ $BRANCH"
echo "  provision.sh: $PROVISION_SCRIPT (embedded)"
if [ "$PUBLIC" = "true" ]; then
    echo "  Domain:       $DOMAIN   (public, TLS via Let's Encrypt)"
    echo "  Public IP:    $PUBLIC_IP"
    echo "  ACME key:     ${ACME_KEY_FILE:-generated during build}"
    echo "  Claim:        ${CLAIM_TOKEN:+token '$CLAIM_TOKEN'}${CLAIM_TOKEN:-token-gated (random, printed on first boot)}"
elif [ "$RASPI" = "true" ]; then
    echo "  Target:       Raspberry Pi SD-card image"
    echo "  Domain:       <hostname>.local, set on first boot   (LAN mode: mDNS, plain http)"
    echo "  Claim:        ${CLAIM_TOKEN:+token '$CLAIM_TOKEN'}${CLAIM_TOKEN:-open (no token required)}"
else
    echo "  Domain:       $DOMAIN   (HTTP-only, bound 0.0.0.0)"
    echo "  Claim:        ${CLAIM_TOKEN:+token '$CLAIM_TOKEN'}${CLAIM_TOKEN:-open (no token required)}"
fi
echo "  Disk size:    $DISK_SIZE"
echo "  Output dir:   $OUTPUT_DIR"
echo ""

# ---- 1. Fetch the Ubuntu base image (cached) ----
if [ "$RASPI" = "true" ]; then
    # The newest 24.04 point release of Ubuntu's preinstalled Raspberry Pi server
    # image, checked against Canonical's published checksum.
    RASPI_RELEASE_URL="https://cdimage.ubuntu.com/releases/noble/release"
    RASPI_SUM_LINE="$(curl -fsSL "$RASPI_RELEASE_URL/SHA256SUMS" \
        | grep 'preinstalled-server-arm64+raspi\.img\.xz$' | sort -k2 -V | tail -n1)"
    [ -n "$RASPI_SUM_LINE" ] || { echo "Error: no Raspberry Pi image in $RASPI_RELEASE_URL/SHA256SUMS" >&2; exit 1; }
    RASPI_XZ_NAME="${RASPI_SUM_LINE##*\*}"
    RASPI_XZ_SHA="${RASPI_SUM_LINE%% *}"
    BASE_IMG="$CACHE_DIR/${RASPI_XZ_NAME%.xz}"
    if [ ! -f "$BASE_IMG" ]; then
        echo "--- Downloading $RASPI_XZ_NAME ---"
        curl -fSL "$RASPI_RELEASE_URL/$RASPI_XZ_NAME" -o "$WORK_DIR/$RASPI_XZ_NAME"
        echo "$RASPI_XZ_SHA  $WORK_DIR/$RASPI_XZ_NAME" | sha256sum -c -
        xz -dc -T0 "$WORK_DIR/$RASPI_XZ_NAME" > "$BASE_IMG.tmp"
        rm -f "$WORK_DIR/$RASPI_XZ_NAME"
        mv "$BASE_IMG.tmp" "$BASE_IMG"
    else
        echo "--- Using cached base image: $BASE_IMG ---"
    fi
else
    BASE_IMG="$CACHE_DIR/noble-server-cloudimg-$ARCH.img"
    if [ ! -f "$BASE_IMG" ]; then
        echo "--- Downloading Ubuntu 24.04 cloud image ---"
        curl -fSL "$CLOUD_IMG_URL" -o "$BASE_IMG.tmp"
        mv "$BASE_IMG.tmp" "$BASE_IMG"
    else
        echo "--- Using cached base image: $BASE_IMG ---"
    fi
fi

# ---- 2. Working disk = copy of base, grown to the target size ----
echo "--- Preparing working disk ($DISK_SIZE) ---"
if [ "$RASPI" = "true" ]; then
    # Raw, since the result is written to an SD card as-is and mtools edits its
    # FAT boot partition in place. cloud-init's growpart fills the extra space
    # with the root partition on the build boot.
    DISK="$WORK_DIR/disk.img"
    DISK_FORMAT="raw"
    cp --sparse=always "$BASE_IMG" "$DISK"
    qemu-img resize -f raw "$DISK" "$DISK_SIZE"
    # mtools addresses the boot partition (partition 1, FAT) by byte offset.
    BOOT_START="$(sfdisk -d "$DISK" | awk -F'[=,]' '$1 ~ /1 :/ { gsub(/ /, "", $2); print $2 }')"
    [ -n "$BOOT_START" ] || { echo "Error: no boot partition found in $BASE_IMG" >&2; exit 1; }
    BOOT_FAT="$DISK@@$((BOOT_START * 512))"
else
    DISK="$WORK_DIR/disk.qcow2"
    DISK_FORMAT="qcow2"
    qemu-img convert -O qcow2 "$BASE_IMG" "$DISK"
    qemu-img resize "$DISK" "$DISK_SIZE"
fi

# ---- 3. Render cloud-init user-data and build the seed ISO ----
echo "--- Building cloud-init seed ---"
SSH_KEY_CONTENT=""
if [ -n "$SSH_PUBKEY_FILE" ]; then
    SSH_KEY_CONTENT="$(cat "$SSH_PUBKEY_FILE")"
fi

# Claim mode. An explicit --claim-token always wins. Otherwise HTTP-only images
# ship open-claim (safe behind NAT); --public images can't (open claiming on a
# reachable instance is refused), so they fall through to provision.sh's default
# random, printed, token-gated claim. This becomes provision.sh args in the seed.
if [ -n "$CLAIM_TOKEN" ]; then
    CLAIM_ARG="--claim-token \"$CLAIM_TOKEN\""
elif [ "$PUBLIC" = "true" ]; then
    CLAIM_ARG=""
else
    CLAIM_ARG="--open-claim"
fi

# Mode args passed to provision.sh: HTTP-only + LAN bind by default, or TLS with
# the baked public IP (and optional ACME account key / email) under --public.
if [ "$PUBLIC" = "true" ]; then
    MODE_ARGS="--public-ip \"$PUBLIC_IP\""
    [ -n "$ACME_KEY_FILE" ] && MODE_ARGS="$MODE_ARGS --acme-key /root/acme_account_key.json"
    [ -n "$ACME_EMAIL" ]    && MODE_ARGS="$MODE_ARGS --acme-email \"$ACME_EMAIL\""
elif [ "$RASPI" = "true" ]; then
    MODE_ARGS=""  # the .local domain selects LAN mode
else
    MODE_ARGS="--local-http-only --bind-host 0.0.0.0"
fi

# Embed provision.sh and seal.sh (and, for --public --acme-key, the account key)
# as single-line base64 blobs. The base64 alphabet is [A-Za-z0-9+/=] — none of
# which collide with sed's '|' delimiter.
b64() { base64 < "$1" | tr -d '\n'; }
PROVISION_B64="$(b64 "$PROVISION_SCRIPT")"
SEAL_B64="$(b64 "$SCRIPT_DIR/seal.sh")"
ACME_KEY_B64=""
[ -n "$ACME_KEY_FILE" ] && ACME_KEY_B64="$(b64 "$ACME_KEY_FILE")"
PRE_PROVISION_B64=""
SEAL_ARGS=""
if [ "$RASPI" = "true" ]; then
    PRE_PROVISION_B64="$(b64 "$SCRIPT_DIR/raspi/pre-provision.sh")"
    SEAL_ARGS="--raspi"
fi

USER_DATA="$WORK_DIR/user-data"
# Use a non-/ delimiter for sed since URLs contain slashes.
sed \
    -e "s|__REPO_URL__|$REPO_URL|g" \
    -e "s|__BRANCH__|$BRANCH|g" \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__MODE_ARGS__|$MODE_ARGS|g" \
    -e "s|__CLAIM_ARG__|$CLAIM_ARG|g" \
    -e "s|__HOST_PASSWORD__|$HOST_PASSWORD|g" \
    -e "s|__SSH_AUTHORIZED_KEY__|$SSH_KEY_CONTENT|g" \
    -e "s|__SWAP_SIZE_GB__|$SWAP_SIZE_GB|g" \
    -e "s|__PROVISION_B64__|$PROVISION_B64|g" \
    -e "s|__SEAL_B64__|$SEAL_B64|g" \
    -e "s|__ACME_KEY_B64__|$ACME_KEY_B64|g" \
    -e "s|__PRE_PROVISION_B64__|$PRE_PROVISION_B64|g" \
    -e "s|__SEAL_ARGS__|$SEAL_ARGS|g" \
    "$SCRIPT_DIR/cloud-init/user-data.tmpl" > "$USER_DATA"

SEED_ISO="$WORK_DIR/seed.iso"
SEED_DRIVE=(-drive "file=$SEED_ISO,if=virtio,format=raw")
case "$SEED_TOOL" in
    cloud-localds)
        cloud-localds "$SEED_ISO" "$USER_DATA" "$SCRIPT_DIR/cloud-init/meta-data"
        ;;
    xorriso)
        xorriso -as genisoimage -output "$SEED_ISO" -volid cidata -joliet -rock \
            "$USER_DATA" "$SCRIPT_DIR/cloud-init/meta-data"
        ;;
    genisoimage|mkisofs)
        "$SEED_TOOL" -output "$SEED_ISO" -volid cidata -joliet -rock \
            "$USER_DATA" "$SCRIPT_DIR/cloud-init/meta-data"
        ;;
    "")
        # --raspi: the Pi image's cloud-init only reads its seed from the boot
        # partition, so swap the build's in there (keeping the stock network and
        # meta-data files to put back afterwards). Also take its kernel + initrd,
        # which QEMU boots directly.
        mcopy -n -i "$BOOT_FAT" ::/network-config ::/meta-data ::/vmlinuz ::/initrd.img "$WORK_DIR/"
        mcopy -o -i "$BOOT_FAT" "$USER_DATA" ::/user-data
        mcopy -o -i "$BOOT_FAT" "$SCRIPT_DIR/cloud-init/meta-data" ::/meta-data
        mcopy -o -i "$BOOT_FAT" "$SCRIPT_DIR/raspi/network-config" ::/network-config
        SEED_DRIVE=()
        ;;
esac

BOOT_ARGS=()
if [ "$RASPI" = "true" ]; then
    BOOT_ARGS=(-kernel "$WORK_DIR/vmlinuz" -initrd "$WORK_DIR/initrd.img"
               -append "root=LABEL=writable rootfstype=ext4 rootwait console=ttyAMA0")
fi

# ---- 4. Boot once under QEMU: cloud-init provisions, then powers off ----
echo "--- Provisioning (booting build VM; this takes a while) ---"
# Persist the guest serial console (kernel + cloud-init + our sentinels) in the
# output dir so a failed build is diagnosable after WORK_DIR is cleaned up.
CONSOLE_LOG="$OUTPUT_DIR/build-console.log"
: > "$CONSOLE_LOG"
echo "  (guest console -> $CONSOLE_LOG)"

# -display none -monitor none: no VGA, no monitor on stdio (nothing waits on
# stdin). The guest serial console (ttyS0, or ttyAMA0 on arm64) is captured
# to CONSOLE_LOG.
set +e
timeout "$BUILD_TIMEOUT" "$QEMU" \
    "${QEMU_MACHINE[@]}" \
    "${KVM_ARGS[@]}" \
    -m "$MEM_MB" \
    -smp "$CPUS" \
    -display none \
    -monitor none \
    -serial "file:$CONSOLE_LOG" \
    ${BOOT_ARGS[@]+"${BOOT_ARGS[@]}"} \
    -drive "file=$DISK,if=virtio,format=$DISK_FORMAT,discard=unmap" \
    ${SEED_DRIVE[@]+"${SEED_DRIVE[@]}"} \
    -netdev user,id=n0 \
    -device virtio-net-pci,netdev=n0 \
    -no-reboot
QEMU_RC=$?
set -e

if [ $QEMU_RC -eq 124 ]; then
    echo "Error: build VM timed out after ${BUILD_TIMEOUT}s. Last console output:" >&2
    tail -n 40 "$CONSOLE_LOG" >&2 || true
    exit 1
fi

if grep -q "BOTTLE_IMAGE_BUILD_SUCCESS" "$CONSOLE_LOG"; then
    echo "  Provisioning succeeded."
elif grep -q "BOTTLE_IMAGE_BUILD_FAILED" "$CONSOLE_LOG"; then
    echo "Error: provisioning reported failure. Last console output:" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
    exit 1
else
    echo "Error: no build sentinel found (VM powered off unexpectedly?). Console:" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
    exit 1
fi

# ---- 5 (--raspi). Restore the boot partition's first-boot config, compress ----
if [ "$RASPI" = "true" ]; then
    echo "--- Finalizing Raspberry Pi image ---"
    # Ship the image's own first-boot user-data (Raspberry Pi Imager replaces it
    # with the user's settings) and the stock network/meta-data files.
    mcopy -o -i "$BOOT_FAT" "$SCRIPT_DIR/raspi/user-data" ::/user-data
    mcopy -o -i "$BOOT_FAT" "$WORK_DIR/network-config" ::/network-config
    mcopy -o -i "$BOOT_FAT" "$WORK_DIR/meta-data" ::/meta-data

    IMG_XZ="$OUTPUT_DIR/$ARTIFACT_BASE.img.xz"
    EXTRACT_SIZE="$(file_size "$DISK")"
    EXTRACT_SHA="$(sha256sum "$DISK" | cut -d' ' -f1)"
    xz -T0 -c "$DISK" > "$IMG_XZ"
    DOWNLOAD_SIZE="$(file_size "$IMG_XZ")"
    DOWNLOAD_SHA="$(sha256sum "$IMG_XZ" | cut -d' ' -f1)"

    # A Raspberry Pi Imager repository listing this image: `rpi-imager --repo
    # <url of this file>` offers it in the OS menu. init_format=cloudinit is what
    # makes Imager write its OS customisation (hostname, wifi, SSH key) as
    # cloud-init config, which this image reads on first boot.
    IMAGER_JSON="$OUTPUT_DIR/$ARTIFACT_BASE.json"
    cat > "$IMAGER_JSON" <<JSON
{
  "os_list": [
    {
      "name": "Cloud in a Bottle $VERSION",
      "description": "Your own cloud on your local network. Reach it at http://<hostname>.local after first boot.",
      "url": "https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/releases/download/$VERSION/$ARTIFACT_BASE.img.xz",
      "release_date": "$(date -u +%Y-%m-%d)",
      "extract_size": $EXTRACT_SIZE,
      "extract_sha256": "$EXTRACT_SHA",
      "image_download_size": $DOWNLOAD_SIZE,
      "image_download_sha256": "$DOWNLOAD_SHA",
      "init_format": "cloudinit",
      "devices": ["pi5-64bit", "pi4-64bit"]
    }
  ]
}
JSON

    echo ""
    echo "  SD-card image: $IMG_XZ"
    echo "  Imager repo:   $IMAGER_JSON"
    echo ""
    echo "=== Build complete ==="
    echo ""
    echo "Write it to an SD card (${DISK_SIZE} or larger) with Raspberry Pi Imager: pick"
    echo "\"Use custom\" (or run \`rpi-imager --repo <url of the .json>\`) and set a"
    echo "hostname, plus wifi if the Pi won't be on ethernet. Then boot the Pi and visit"
    echo "    http://<hostname>.local   (default hostname: bottle)"
    if [ -n "$CLAIM_TOKEN" ]; then
        echo "and claim it at /setup?claim=$CLAIM_TOKEN"
    else
        echo "and claim it at /setup (open, no token)."
    fi
    exit 0
fi

# ---- 5. Compact the qcow2 (drop freed blocks) ----
echo "--- Finalizing qcow2 ---"
QCOW2_OUT="$OUTPUT_DIR/$ARTIFACT_BASE.qcow2"
qemu-img convert -O qcow2 -c "$DISK" "$QCOW2_OUT"

echo ""
echo "  QEMU image:   $QCOW2_OUT"

# ---- 6. VirtualBox OVA (qcow2 -> streamOptimized VMDK -> OVF -> tar) ----
if [ "$MAKE_OVA" = "true" ]; then
    echo "--- Building VirtualBox OVA ---"
    OVA_STAGE="$WORK_DIR/ova"
    mkdir -p "$OVA_STAGE"
    VMDK="$OVA_STAGE/$ARTIFACT_BASE.vmdk"
    qemu-img convert -O vmdk -o subformat=streamOptimized,adapter_type=lsilogic \
        "$DISK" "$VMDK"

    CAPACITY_BYTES="$(qemu-img info --output=json "$DISK" | sed -n 's/.*"virtual-size": *\([0-9]*\).*/\1/p' | head -n1)"
    VMDK_BYTES="$(file_size "$VMDK")"
    OVF="$OVA_STAGE/$ARTIFACT_BASE.ovf"
    VMDK_NAME="$(basename "$VMDK")"

    cat > "$OVF" <<OVF_EOF
<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
          xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <References>
    <File ovf:href="$VMDK_NAME" ovf:id="file1" ovf:size="$VMDK_BYTES"/>
  </References>
  <DiskSection>
    <Info>Virtual disk information</Info>
    <Disk ovf:capacity="$CAPACITY_BYTES" ovf:diskId="vmdisk1" ovf:fileRef="file1"
          ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
  </DiskSection>
  <NetworkSection>
    <Info>Logical networks</Info>
    <Network ovf:name="NAT">
      <Description>NAT network</Description>
    </Network>
  </NetworkSection>
  <VirtualSystem ovf:id="cloud-in-a-bottle-$VERSION">
    <Info>Cloud in a Bottle $VERSION</Info>
    <Name>cloud-in-a-bottle-$VERSION</Name>
    <OperatingSystemSection ovf:id="94">
      <Info>Ubuntu 24.04 (64-bit)</Info>
      <Description>Ubuntu_64</Description>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware requirements</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemType>virtualbox-2.2</vssd:VirtualSystemType>
      </System>
      <Item>
        <rasd:Caption>$CPUS virtual CPU(s)</rasd:Caption>
        <rasd:Description>Number of virtual CPUs</rasd:Description>
        <rasd:ElementName>$CPUS virtual CPU(s)</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>$CPUS</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>MegaBytes</rasd:AllocationUnits>
        <rasd:Caption>$MEM_MB MB of memory</rasd:Caption>
        <rasd:Description>Memory Size</rasd:Description>
        <rasd:ElementName>$MEM_MB MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>$MEM_MB</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Address>0</rasd:Address>
        <rasd:Caption>sataController0</rasd:Caption>
        <rasd:Description>SATA Controller</rasd:Description>
        <rasd:ElementName>sataController0</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>AHCI</rasd:ResourceSubType>
        <rasd:ResourceType>20</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:Caption>disk1</rasd:Caption>
        <rasd:Description>Disk Image</rasd:Description>
        <rasd:ElementName>disk1</rasd:ElementName>
        <rasd:HostResource>/disk/vmdisk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
        <rasd:Caption>Ethernet adapter on 'NAT'</rasd:Caption>
        <rasd:Connection>NAT</rasd:Connection>
        <rasd:ElementName>Ethernet adapter on 'NAT'</rasd:ElementName>
        <rasd:InstanceID>5</rasd:InstanceID>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
OVF_EOF

    OVA_OUT="$OUTPUT_DIR/$ARTIFACT_BASE.ova"
    # OVA spec: the .ovf must be the first entry in the tar, disk(s) after.
    tar -C "$OVA_STAGE" -cf "$OVA_OUT" "$(basename "$OVF")" "$VMDK_NAME"
    echo "  VirtualBox:   $OVA_OUT"
fi

echo ""
echo "=== Build complete ==="
echo ""
echo "Disk:   ships as ${DISK_SIZE} (floor). To install with more, size the VM's"
echo "        virtual disk larger before first boot (or write to a bigger"
echo "        physical disk) — the root filesystem grows to fill it on boot."
echo ""
if [ "$PUBLIC" = "true" ]; then
    echo "Public image for: $DOMAIN"
    echo "Before it can serve, delegate DNS to $PUBLIC_IP and open ports 53, 80, 443"
    echo "to the VM. Then the dashboard comes up at:  https://$DOMAIN"
    if [ -n "$CLAIM_TOKEN" ]; then
        echo "Claim URL:                                  https://$DOMAIN/setup?claim=$CLAIM_TOKEN"
    else
        echo "Claim:                                      token-gated; the random token prints"
        echo "                                            to the instance console on first boot."
    fi
else
    echo "Boot it, then reach the dashboard at:  http://<vm-ip>:8080"
    if [ -n "$CLAIM_TOKEN" ]; then
        echo "Claim URL:                             http://<vm-ip>:8080/setup?claim=$CLAIM_TOKEN"
    else
        echo "Claim:                                 open — go to /setup (no token)"
    fi
    echo "Find <vm-ip> from the VM console (\`ip addr\`) or your hypervisor's NAT/DHCP."
fi
echo "Console login:                         user 'host', password '$HOST_PASSWORD'"
