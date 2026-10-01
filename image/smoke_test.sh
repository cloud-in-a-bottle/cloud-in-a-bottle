#!/usr/bin/env bash
# smoke_test.sh: Boot a built OpenHost image and check that it actually works.
#
# Boots the qcow2 (through a throwaway overlay, so the image is untouched),
# claims it through the open /setup flow, then waits for every default app to
# build and reach "running". That exercises the router, podman image builds and
# container networking on the image's own architecture, which is the part a
# successful build boot does not prove.
#
# Usage (run on a Linux host, ideally with KVM for the image's arch):
#   image/smoke_test.sh [options] <image.qcow2>
#
# Options:
#   --arch <arch>         Image architecture: amd64 or arm64 (default: the host's)
#   --mem <mb>            VM memory in MB (default: 4096)
#   --cpus <n>            VM vCPUs (default: 2)
#   --port <port>         Host port forwarded to the dashboard (default: 18080)
#   --domain <domain>     Domain the image was built with (default: lvh.me)
#   --timeout <sec>       Max seconds for boot plus app deploys (default: 2400)
#   --expect-apps <n>     Number of apps that must reach running (default: 6,
#                         the length of default_apps in compute_space/config.py)
#   -h, --help            Show this help

set -euo pipefail

case "$(uname -m)" in
    aarch64|arm64) ARCH="arm64" ;;
    *)             ARCH="amd64" ;;
esac
MEM_MB="4096"
CPUS="2"
PORT="18080"
DOMAIN="lvh.me"
TIMEOUT="2400"
EXPECT_APPS="6"
IMAGE=""
OWNER_PASSWORD="smoke-test-password"

usage() { sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "${BASH_SOURCE[0]}"; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --arch)     ARCH="$2"; shift 2 ;;
        --mem)      MEM_MB="$2"; shift 2 ;;
        --cpus)     CPUS="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --domain)   DOMAIN="$2"; shift 2 ;;
        --timeout)  TIMEOUT="$2"; shift 2 ;;
        --expect-apps) EXPECT_APPS="$2"; shift 2 ;;
        -h|--help)  usage; exit 0 ;;
        -*)         echo "Unknown option: $1" >&2; usage; exit 1 ;;
        *)          IMAGE="$1"; shift ;;
    esac
done

if [ -z "$IMAGE" ] || [ ! -f "$IMAGE" ]; then
    echo "Error: pass the path to a built .qcow2 image." >&2
    usage
    exit 1
fi

HOST_ARCH="$(uname -m)"
case "$ARCH" in
    amd64)
        QEMU="qemu-system-x86_64"
        QEMU_MACHINE=()
        NATIVE="$([ "$HOST_ARCH" = "x86_64" ] && echo true || echo false)"
        ;;
    arm64)
        QEMU="qemu-system-aarch64"
        UEFI_FW=""
        for f in /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
                 /usr/share/AAVMF/AAVMF_CODE.fd \
                 /usr/share/qemu/edk2-aarch64-code.fd \
                 /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
                 /usr/local/share/qemu/edk2-aarch64-code.fd; do
            if [ -f "$f" ]; then UEFI_FW="$f"; break; fi
        done
        if [ -z "$UEFI_FW" ]; then
            echo "Error: no aarch64 UEFI firmware found. Install qemu-efi-aarch64." >&2
            exit 1
        fi
        QEMU_MACHINE=(-machine virt -bios "$UEFI_FW")
        NATIVE="$([ "$HOST_ARCH" = "aarch64" ] && echo true || echo false)"
        ;;
    *)
        echo "Error: --arch must be amd64 or arm64 (got '$ARCH')." >&2
        exit 1
        ;;
esac

if [ "$NATIVE" = "true" ] && [ -e /dev/kvm ] && [ -w /dev/kvm ]; then
    ACCEL_ARGS=(-enable-kvm -cpu host)
else
    echo "(no usable KVM for $ARCH on this host; falling back to slow TCG emulation)"
    # pauth-impdef swaps aarch64 pointer authentication's architected
    # algorithm (very slow to emulate) for a cheap one; the guest can't tell.
    TCG_CPU="max"
    [ "$ARCH" = "arm64" ] && TCG_CPU="max,pauth-impdef=on"
    ACCEL_ARGS=(-accel tcg,thread=multi -cpu "$TCG_CPU")
fi

WORK_DIR="$(mktemp -d)"
QEMU_PID=""
cleanup() {
    if [ -n "$QEMU_PID" ]; then kill "$QEMU_PID" 2>/dev/null || true; fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

CONSOLE_LOG="$(dirname "$IMAGE")/smoke-console.log"
: > "$CONSOLE_LOG"
OVERLAY="$WORK_DIR/overlay.qcow2"
qemu-img create -q -f qcow2 -F qcow2 -b "$(realpath "$IMAGE")" "$OVERLAY"

echo "=== Smoke testing $IMAGE ($ARCH) ==="
echo "  (guest console -> $CONSOLE_LOG)"
"$QEMU" \
    "${QEMU_MACHINE[@]}" \
    "${ACCEL_ARGS[@]}" \
    -m "$MEM_MB" \
    -smp "$CPUS" \
    -display none \
    -monitor none \
    -serial "file:$CONSOLE_LOG" \
    -drive "file=$OVERLAY,if=virtio,format=qcow2" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:8080" \
    -device virtio-net-pci,netdev=n0 &
QEMU_PID=$!

# The session cookie is scoped to the instance's domain, so talk to the router
# by that name (pinned to the forwarded port, no DNS needed) rather than by IP,
# or curl never sends the cookie back.
BASE="http://$DOMAIN:$PORT"
JAR="$WORK_DIR/cookies"
curl() { command curl --resolve "$DOMAIN:$PORT:127.0.0.1" "$@"; }
DEADLINE=$(( $(date +%s) + TIMEOUT ))

fail() {
    echo "FAIL: $1" >&2
    echo "--- last guest console output ---" >&2
    tail -n 60 "$CONSOLE_LOG" >&2 || true
    exit 1
}

# Poll until $1 returns HTTP 200, or fail with $2.
wait_ok() {
    until [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || true)" = "200" ]; do
        kill -0 "$QEMU_PID" 2>/dev/null || fail "VM exited while waiting for $1"
        [ "$(date +%s)" -lt "$DEADLINE" ] || fail "$2"
        sleep 5
    done
}

echo "--- Waiting for the dashboard to come up ---"
wait_ok "$BASE/health" "router never answered /health"

echo "--- Claiming the instance via /setup ---"
SETUP_PAGE="$WORK_DIR/setup.html"
code="$(curl -s -o "$SETUP_PAGE" -w '%{http_code}' -c "$JAR" -b "$JAR" \
    --data-urlencode "password=$OWNER_PASSWORD" \
    --data-urlencode "confirm_password=$OWNER_PASSWORD" \
    "$BASE/setup")"
[ "$code" = "200" ] || fail "POST /setup returned $code: $(head -c 500 "$SETUP_PAGE")"
grep -q "Setup complete" "$SETUP_PAGE" || fail "POST /setup did not return the readiness page"

# Setup restarts the router into the full app; wait for it to come back.
sleep 5
wait_ok "$BASE/health" "router did not come back after setup"

echo "--- Waiting for the default apps to build and start ---"
last=""
while true; do
    kill -0 "$QEMU_PID" 2>/dev/null || fail "VM exited while waiting for apps"
    apps="$WORK_DIR/apps.json"
    code="$(curl -s -o "$apps" -w '%{http_code}' --max-time 10 -b "$JAR" "$BASE/api/apps" || true)"
    case "$code" in
        401|403|302|303) fail "GET /api/apps returned $code: the setup session was not accepted" ;;
    esac
    # Prints one "name status" line per app, then a verdict line: "done",
    # "error <name>: <message>", or "wait".
    summary="$(EXPECT_APPS="$EXPECT_APPS" python3 -c '
import json, os, sys
try:
    apps = json.load(sys.stdin)
except ValueError:
    print("wait")
    sys.exit()
for a in apps:
    print(a["name"], a["status"])
bad = [a for a in apps if a["status"] == "error"]
if bad:
    print("error %s: %s" % (bad[0]["name"], bad[0]["error_message"]))
elif len(apps) >= int(os.environ["EXPECT_APPS"]) and all(a["status"] == "running" for a in apps):
    print("done")
else:
    print("wait")
' < "$apps")"
    if [ "$summary" != "$last" ]; then
        echo "$summary" | sed '$d' | sed 's/^/  /'
        echo "  --"
        last="$summary"
    fi
    verdict="$(echo "$summary" | tail -n1)"
    case "$verdict" in
        done)    break ;;
        error*)  fail "app deploy failed: ${verdict#error }" ;;
    esac
    [ "$(date +%s)" -lt "$DEADLINE" ] || fail "default apps did not all reach running within ${TIMEOUT}s"
    sleep 10
done

echo ""
echo "=== Smoke test passed: all default apps running on $ARCH ==="
