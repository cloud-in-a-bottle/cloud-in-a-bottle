#!/usr/bin/env bash
# smoke_test.sh: boot a built Raspberry Pi image (build.sh --raspi) the way a user's first boot goes,
# and check it comes up at the hostname they picked.
#
# Like Raspberry Pi Imager, it writes a user-data with a custom hostname onto the boot partition of a
# copy of the image, then boots that copy under KVM (the Pi kernel on QEMU's generic virt machine,
# as the build does) and checks that:
#   - the dashboard answers at http://<hostname>.local (so the first-boot unit picked up the
#     hostname, and the router, Caddy and the open /setup are up), and
#   - the mDNS responder answers for an app subdomain of it.
#
# Usage: image/raspi/smoke_test.sh <image.img.xz> [--timeout <sec>]
#
# Needs: qemu-system-aarch64, KVM, mtools, sfdisk, xz, curl, python3. The guest console is saved to
# image/out/smoke-console.log.

set -euo pipefail

IMG_XZ="${1:?usage: smoke_test.sh <image.img.xz> [--timeout <sec>]}"
TIMEOUT=900
[ "${2:-}" = "--timeout" ] && TIMEOUT="$3"

HOSTNAME_UNDER_TEST="smoketest"
HTTP_PORT=18080
MDNS_PORT=15353
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

# What Imager's OS customisation would write (it sets far more, but the hostname is what matters).
cat > "$WORK_DIR/user-data" <<EOF
#cloud-config
hostname: $HOSTNAME_UNDER_TEST
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
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$HTTP_PORT-:80,hostfwd=udp:127.0.0.1:$MDNS_PORT-:5353" \
    -device virtio-net-pci,netdev=n0 &
QEMU_PID=$!

fail() {
    echo "Error: $1. Last console output:" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
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

echo "--- Querying mDNS for myapp.$HOSTNAME_UNDER_TEST.local ---"
# A legacy (non-5353 source port) query, which the responder answers by unicast, so it comes back
# through QEMU's user-mode NAT. The answer is the guest's address on the interface it arrived on.
mdns_query() {
python3 - "$MDNS_PORT" "$1" <<'PY'
import socket, struct, sys

port, name = int(sys.argv[1]), sys.argv[2]
qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
query = struct.pack("!HHHHHH", 0x1234, 0, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.settimeout(5)
    for _ in range(5):
        s.sendto(query, ("127.0.0.1", port))
        try:
            reply = s.recv(9000)
            break
        except socket.timeout:
            continue
    else:
        sys.exit("no mDNS reply")
_, flags, qd, an, _, _ = struct.unpack_from("!HHHHHH", reply)
if not (flags & 0x8000) or an < 1:
    sys.exit(f"unexpected mDNS reply: {reply!r}")
# The single echoed question is uncompressed and identical to ours; the first answer follows it.
off = 12 + len(qname) + 4
off += len(qname)
rtype, _, _, rdlen = struct.unpack_from("!HHIH", reply, off)
ip = socket.inet_ntoa(reply[off + 10 : off + 10 + rdlen])
if rtype != 1:
    sys.exit(f"first answer is type {rtype}, not A")
print(f"  {name} -> {ip}")
PY
}
if ! mdns_query "myapp.$HOSTNAME_UNDER_TEST.local"; then
    # Tell a domain that wasn't taken from the hostname apart from a query that never reached us.
    if mdns_query "myapp.bottle.local"; then
        fail "the responder still publishes the default bottle.local, so the domain wasn't taken from the hostname"
    fi
    fail "no mDNS answer for either name"
fi

# The responder only publishes names under the instance's configured domains, so that answer also
# shows the first-boot unit set the domain from the hostname.
echo "=== Smoke test passed ==="
