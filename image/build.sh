#!/usr/bin/env bash
# build.sh — Build a bootable OpenHost VM image from the Ubuntu 24.04 cloud image.
#
# The recipe is deliberately plain: take Ubuntu's official cloud-image qcow2,
# boot it once under QEMU with a cloud-init seed that runs our existing,
# tested provision.sh, let it power itself off, then freeze the resulting disk.
# No Packer, no autoinstall ISO dance — just the exact code path a real deploy
# uses. Output is a QEMU qcow2 and (optionally) a VirtualBox OVA.
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
# Usage (run on a Linux host with KVM):
#   image/build.sh [options]
#
# Options:
#   --branch <branch>     Git branch of openhost app code to clone (default: main)
#   --repo <url>          Git repo URL to clone app code from
#                         (default: imbue-openhost/openhost)
#   --provision-script <path>
#                         provision.sh to embed and run in the build VM
#                         (default: this repo's scripts/provision.sh). Embedded
#                         from the working tree, so the branch need not be pushed.
#   --domain <domain>     App subdomain-routing domain baked in (default: lvh.me).
#                         With --public this is the real domain served over TLS.
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
#                         (default: openhost)
#   --ssh-pubkey <path>   Optional SSH public key file to authorize for `host`
#                         (SSH is key-only; without this, access is console-only)
#   --version <v>         Version string used in artifact filenames
#                         (default: `git describe` or "dev")
#   --disk-size <size>    Virtual disk size baked into the image — the default
#                         floor only (default: 20G). The image grows its root
#                         filesystem to fill whatever disk it is installed onto
#                         on first boot, so users pick the real size by sizing
#                         the VM disk (or the physical disk on bare metal).
#   --swap-size <gib>     Swap file size in GiB baked into the image (default: 2)
#   --mem <mb>            Build VM memory in MB (default: 4096)
#   --cpus <n>            Build VM vCPUs (default: 2)
#   --output-dir <dir>    Where artifacts land (default: image/out)
#   --no-ova              Skip the VirtualBox OVA; produce only the qcow2
#   --timeout <sec>       Max seconds to wait for the build boot (default: 1800)
#   -h, --help            Show this help
#
# Requirements: qemu-system-x86_64, qemu-img, cloud-localds (cloud-image-utils)
# or genisoimage/xorriso, curl, tar. KVM (/dev/kvm) strongly recommended —
# without it the build boot falls back to slow TCG emulation.

set -euo pipefail

# ---- Defaults ----
BRANCH="main"
REPO_URL="https://github.com/imbue-openhost/openhost.git"
DOMAIN="lvh.me"
CLAIM_TOKEN=""   # empty => open claim (no token required); set to bake a token
HOST_PASSWORD="openhost"
SSH_PUBKEY_FILE=""
VERSION=""
DISK_SIZE="20G"
SWAP_SIZE_GB="2"
MEM_MB="4096"
CPUS="2"
BUILD_TIMEOUT="1800"
MAKE_OVA="true"
PUBLIC="false"
PUBLIC_IP=""
ACME_KEY_FILE=""
ACME_EMAIL=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/out"
CACHE_DIR="$SCRIPT_DIR/cache"
PROVISION_SCRIPT="$SCRIPT_DIR/../scripts/provision.sh"

CLOUD_IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

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
        --password)     HOST_PASSWORD="$2"; shift 2 ;;
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
        -h|--help)      usage; exit 0 ;;
        *)              echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

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
need qemu-system-x86_64 "Install qemu-system-x86."
need curl           "Install curl."
need tar            "Install tar."

# Seed-ISO builder: prefer cloud-localds, fall back to xorriso/genisoimage.
SEED_TOOL=""
if command -v cloud-localds >/dev/null 2>&1; then
    SEED_TOOL="cloud-localds"
elif command -v xorriso >/dev/null 2>&1; then
    SEED_TOOL="xorriso"
elif command -v genisoimage >/dev/null 2>&1; then
    SEED_TOOL="genisoimage"
else
    echo "Error: need one of cloud-localds (cloud-image-utils), xorriso, or genisoimage." >&2
    exit 1
fi

if [ -z "$VERSION" ]; then
    VERSION="$(git -C "$SCRIPT_DIR" describe --tags --always --dirty 2>/dev/null || echo dev)"
fi

if [ ! -f "$PROVISION_SCRIPT" ]; then
    echo "Error: provision script not found: $PROVISION_SCRIPT" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR" "$CACHE_DIR"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "=== OpenHost VM image build ==="
echo "  Version:      $VERSION"
echo "  Repo/branch:  $REPO_URL @ $BRANCH"
echo "  provision.sh: $PROVISION_SCRIPT (embedded)"
if [ "$PUBLIC" = "true" ]; then
    echo "  Domain:       $DOMAIN   (public, TLS via Let's Encrypt)"
    echo "  Public IP:    $PUBLIC_IP"
    echo "  ACME key:     ${ACME_KEY_FILE:-generated during build}"
    echo "  Claim:        ${CLAIM_TOKEN:+token '$CLAIM_TOKEN'}${CLAIM_TOKEN:-token-gated (random, printed on first boot)}"
else
    echo "  Domain:       $DOMAIN   (HTTP-only, bound 0.0.0.0)"
    echo "  Claim:        ${CLAIM_TOKEN:+token '$CLAIM_TOKEN'}${CLAIM_TOKEN:-open (no token required)}"
fi
echo "  Disk size:    $DISK_SIZE"
echo "  Output dir:   $OUTPUT_DIR"
echo ""

# ---- 1. Fetch the Ubuntu cloud base image (cached) ----
BASE_IMG="$CACHE_DIR/noble-server-cloudimg-amd64.img"
if [ ! -f "$BASE_IMG" ]; then
    echo "--- Downloading Ubuntu 24.04 cloud image ---"
    curl -fSL "$CLOUD_IMG_URL" -o "$BASE_IMG.tmp"
    mv "$BASE_IMG.tmp" "$BASE_IMG"
else
    echo "--- Using cached base image: $BASE_IMG ---"
fi

# ---- 2. Working disk = copy of base, grown to the target size ----
DISK="$WORK_DIR/disk.qcow2"
echo "--- Preparing working disk ($DISK_SIZE) ---"
qemu-img convert -O qcow2 "$BASE_IMG" "$DISK"
qemu-img resize "$DISK" "$DISK_SIZE"

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
else
    MODE_ARGS="--local-http-only --bind-host 0.0.0.0"
fi

# Embed provision.sh and seal.sh (and, for --public --acme-key, the account key)
# as single-line base64 blobs. The base64 alphabet is [A-Za-z0-9+/=] — none of
# which collide with sed's '|' delimiter.
PROVISION_B64="$(base64 -w0 "$PROVISION_SCRIPT")"
SEAL_B64="$(base64 -w0 "$SCRIPT_DIR/seal.sh")"
ACME_KEY_B64=""
[ -n "$ACME_KEY_FILE" ] && ACME_KEY_B64="$(base64 -w0 "$ACME_KEY_FILE")"

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
    "$SCRIPT_DIR/cloud-init/user-data.tmpl" > "$USER_DATA"

SEED_ISO="$WORK_DIR/seed.iso"
case "$SEED_TOOL" in
    cloud-localds)
        cloud-localds "$SEED_ISO" "$USER_DATA" "$SCRIPT_DIR/cloud-init/meta-data"
        ;;
    xorriso)
        xorriso -as genisoimage -output "$SEED_ISO" -volid cidata -joliet -rock \
            "$USER_DATA" "$SCRIPT_DIR/cloud-init/meta-data"
        ;;
    genisoimage)
        genisoimage -output "$SEED_ISO" -volid cidata -joliet -rock \
            "$USER_DATA" "$SCRIPT_DIR/cloud-init/meta-data"
        ;;
esac

# ---- 4. Boot once under QEMU: cloud-init provisions, then powers off ----
echo "--- Provisioning (booting build VM; this takes a while) ---"
# Persist the guest serial console (kernel + cloud-init + our sentinels) in the
# output dir so a failed build is diagnosable after WORK_DIR is cleaned up.
CONSOLE_LOG="$OUTPUT_DIR/build-console.log"
: > "$CONSOLE_LOG"
echo "  (guest console -> $CONSOLE_LOG)"

KVM_ARGS=()
if [ -e /dev/kvm ] && [ -w /dev/kvm ]; then
    KVM_ARGS=(-enable-kvm -cpu host)
else
    echo "  (no writable /dev/kvm — falling back to slow TCG emulation)"
    KVM_ARGS=(-cpu max)
fi

# -display none -monitor none: no VGA, no monitor on stdio (nothing waits on
# stdin). The guest's ttyS0 console is captured to CONSOLE_LOG.
set +e
timeout "$BUILD_TIMEOUT" qemu-system-x86_64 \
    "${KVM_ARGS[@]}" \
    -m "$MEM_MB" \
    -smp "$CPUS" \
    -display none \
    -monitor none \
    -serial "file:$CONSOLE_LOG" \
    -drive "file=$DISK,if=virtio,format=qcow2" \
    -drive "file=$SEED_ISO,if=virtio,format=raw" \
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

if grep -q "OPENHOST_IMAGE_BUILD_SUCCESS" "$CONSOLE_LOG"; then
    echo "  Provisioning succeeded."
elif grep -q "OPENHOST_IMAGE_BUILD_FAILED" "$CONSOLE_LOG"; then
    echo "Error: provisioning reported failure. Last console output:" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
    exit 1
else
    echo "Error: no build sentinel found (VM powered off unexpectedly?). Console:" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
    exit 1
fi

# ---- 5. Compact the qcow2 (drop freed blocks) ----
echo "--- Finalizing qcow2 ---"
QCOW2_OUT="$OUTPUT_DIR/openhost-$VERSION-amd64.qcow2"
qemu-img convert -O qcow2 -c "$DISK" "$QCOW2_OUT"

echo ""
echo "  QEMU image:   $QCOW2_OUT"

# ---- 6. VirtualBox OVA (qcow2 -> streamOptimized VMDK -> OVF -> tar) ----
if [ "$MAKE_OVA" = "true" ]; then
    echo "--- Building VirtualBox OVA ---"
    OVA_STAGE="$WORK_DIR/ova"
    mkdir -p "$OVA_STAGE"
    VMDK="$OVA_STAGE/openhost-$VERSION-amd64.vmdk"
    qemu-img convert -O vmdk -o subformat=streamOptimized,adapter_type=lsilogic \
        "$DISK" "$VMDK"

    CAPACITY_BYTES="$(qemu-img info --output=json "$DISK" | sed -n 's/.*"virtual-size": *\([0-9]*\).*/\1/p' | head -n1)"
    VMDK_BYTES="$(file_size "$VMDK")"
    OVF="$OVA_STAGE/openhost-$VERSION-amd64.ovf"
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
  <VirtualSystem ovf:id="openhost-$VERSION">
    <Info>OpenHost $VERSION</Info>
    <Name>openhost-$VERSION</Name>
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

    OVA_OUT="$OUTPUT_DIR/openhost-$VERSION-amd64.ova"
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
