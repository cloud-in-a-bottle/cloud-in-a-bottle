#!/usr/bin/env bash
# smoke_test.sh: boot a built Raspberry Pi image (build.sh --raspi) the way a user's first boot goes,
# and check it comes up at the hostname they picked.
#
# Like Raspberry Pi Imager, it writes a user-data with a custom hostname onto the boot partition of a
# copy of the image, then boots that copy under KVM (the Pi kernel on QEMU's generic virt machine,
# as the build does) and checks that:
#   - the dashboard answers at http://<hostname>.local (so the first-boot unit picked up the
#     hostname, and the router, Caddy and the open /setup are up), and
#   - the mDNS responder answers a multicast query for an app subdomain of it, sent from inside the
#     guest (over SSH, with a key set in the user-data as Imager would) like a LAN client's.
#
# Usage: image/raspi/smoke_test.sh <image.img.xz> [--timeout <sec>]
#
# Needs: qemu-system-aarch64, KVM, mtools, sfdisk, xz, curl, ssh. The guest console is saved to
# image/out/smoke-console.log.

set -euo pipefail

IMG_XZ="${1:?usage: smoke_test.sh <image.img.xz> [--timeout <sec>]}"
TIMEOUT=900
[ "${2:-}" = "--timeout" ] && TIMEOUT="$3"

HOSTNAME_UNDER_TEST="smoketest"
HTTP_PORT=18080
SSH_PORT=12222
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR/../out"
CONSOLE_LOG="$OUT_DIR/smoke-console.log"
WORK_DIR="$(mktemp -d)"
QEMU_PID=""
cleanup() {
    [ -n "$QEMU_PID" ] && kill "$QEMU_PID" 2>/dev/null || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT
mkdir -p "$OUT_DIR"

echo "--- Unpacking $IMG_XZ ---"
DISK="$WORK_DIR/disk.img"
xz -dc -T0 "$IMG_XZ" | dd of="$DISK" bs=1M iflag=fullblock conv=sparse status=none
BOOT_START="$(sfdisk -d "$DISK" | awk -F'[=,]' '$1 ~ /1 :/ { gsub(/ /, "", $2); print $2 }')"
BOOT_FAT="$DISK@@$((BOOT_START * 512))"

# What Imager's OS customisation would write, minus wifi: a hostname and an SSH key for the default
# user.
ssh-keygen -q -t ed25519 -N "" -f "$WORK_DIR/id"
cat > "$WORK_DIR/user-data" <<EOF
#cloud-config
hostname: $HOSTNAME_UNDER_TEST
ssh_authorized_keys:
  - $(cat "$WORK_DIR/id.pub")
EOF
mcopy -o -i "$BOOT_FAT" "$WORK_DIR/user-data" ::/user-data
mcopy -n -i "$BOOT_FAT" ::/vmlinuz ::/initrd.img "$WORK_DIR/"

echo "--- Booting (console -> $CONSOLE_LOG) ---"
# net.ifnames=0 names the NIC eth0, which the image's stock network-config matches (as on a Pi).
qemu-system-aarch64 \
    -machine virt -enable-kvm -cpu host -m 4096 -smp 4 \
    -display none -monitor none -serial "file:$CONSOLE_LOG" \
    -kernel "$WORK_DIR/vmlinuz" -initrd "$WORK_DIR/initrd.img" \
    -append "root=LABEL=writable rootfstype=ext4 rootwait console=ttyAMA0 net.ifnames=0" \
    -drive "file=$DISK,if=virtio,format=raw" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$HTTP_PORT-:80,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" \
    -device virtio-net-pci,netdev=n0 &
QEMU_PID=$!

guest() {
    ssh -i "$WORK_DIR/id" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=10 ubuntu@127.0.0.1 "$@"
}

fail() {
    echo "Error: $1. Last console output:" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
    echo "--- Guest diagnostics ---" >&2
    guest 'sudo cat /home/host/.openhost/local_compute_space/first_boot.toml; sudo ss -ulpn;
        sudo journalctl -b -u openhost --no-pager | grep -iE "mdns|error|traceback" | tail -n 40' >&2 || true
    exit 1
}

echo "--- Waiting for http://$HOSTNAME_UNDER_TEST.local/ ---"
deadline=$((SECONDS + TIMEOUT))
code=""
while [ $SECONDS -lt $deadline ]; do
    kill -0 "$QEMU_PID" 2>/dev/null || fail "the VM exited"
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HOSTNAME_UNDER_TEST.local" \
        "http://127.0.0.1:$HTTP_PORT/setup" || true)"
    [ "$code" = "200" ] && break
    sleep 5
done
[ "$code" = "200" ] || fail "no 200 from /setup on $HOSTNAME_UNDER_TEST.local within ${TIMEOUT}s (last: ${code:-none})"
echo "  /setup answers on $HOSTNAME_UNDER_TEST.local"

echo "--- Querying mDNS for myapp.$HOSTNAME_UNDER_TEST.local, from the guest ---"
if ! guest python3 - "myapp.$HOSTNAME_UNDER_TEST.local" < "$SCRIPT_DIR/mdns_query.py"; then
    # Tell a domain that wasn't taken from the hostname apart from a responder that isn't answering.
    if guest python3 - "myapp.bottle.local" < "$SCRIPT_DIR/mdns_query.py"; then
        fail "the responder still publishes the default bottle.local, so the domain wasn't taken from the hostname"
    fi
    fail "no mDNS answer for either name"
fi
# The responder only publishes names under the instance's configured domains, so that answer also
# shows the first-boot unit set the domain from the hostname.
echo "=== Smoke test passed ==="
