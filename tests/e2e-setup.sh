#!/bin/bash
# Unified e2e setup: create instance, deploy Cloud in a Bottle via ansible, verify.
#
# Provider-agnostic — the cloud provider is specified via the PROVIDER env var
# (or first argument). Each provider implements a simple interface in
# tests/providers/<provider>.sh.
#
# Usage:
#   PROVIDER=gcp tests/e2e-setup.sh [subdomain]
#   PROVIDER=ec2 tests/e2e-setup.sh [subdomain]
#
# Outputs an env file (default: /tmp/e2e_env.sh) consumed by tests and teardown.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PROVIDER="${PROVIDER:?PROVIDER is required (gcp, ec2, ...)}"
PROVIDER_SCRIPT="$SCRIPT_DIR/providers/${PROVIDER}.sh"

if [ ! -f "$PROVIDER_SCRIPT" ]; then
    echo "ERROR: Unknown provider '$PROVIDER' (no file at $PROVIDER_SCRIPT)" >&2
    exit 1
fi

source "$SCRIPT_DIR/common.sh"
source "$PROVIDER_SCRIPT"

ENV_FILE="${E2E_ENV_FILE:-/tmp/e2e_env.sh}"
SSH_KEY="${E2E_SSH_KEY:?E2E_SSH_KEY is required}"
SSH_USER="${E2E_SSH_USER:-ubuntu}"

# ── Determine TLS mode ───────────────────────────────────────────────────

USE_TLS=false
DOMAIN=""
RUN_ID=""
if [ -n "${E2E_HOSTED_ZONE_ID:-}" ] && [ -n "${E2E_BASE_DOMAIN:-}" ]; then
    USE_TLS=true
    RUN_ID="${1:-$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n')}"
    DOMAIN="openhost-e2e-${RUN_ID}.${E2E_BASE_DOMAIN}"
fi

# Known claim token so the python test can drive /setup. run_ansible_setup
# threads this into the playbook; the test reads OPENHOST_CLAIM_TOKEN.
export CLAIM_TOKEN="${CLAIM_TOKEN:-$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')}"

echo "=== Setting up Cloud in a Bottle on $PROVIDER ==="
if $USE_TLS; then
    echo "  Domain: $DOMAIN (TLS mode)"
else
    echo "  Domain: none (HTTP mode)"
fi

# ── 1. Create instance via provider ──────────────────────────────────────

echo ""
echo "--- Creating $PROVIDER instance ---"
# Call provider_create in the current shell (not a subshell) so that internal
# state variables (_EC2_INSTANCE_ID, _GCP_INSTANCE_NAME, etc.) are preserved
# for provider_env_vars and teardown.
provider_create "${RUN_ID:-manual}" "$SSH_KEY"
PUBLIC_IP="$PROVIDER_PUBLIC_IP"
echo "  Public IP: $PUBLIC_IP"

# ── 2. Write env file (early, so teardown works if later steps fail) ─────
# Write base vars and provider-specific vars in a single atomic operation
# so that cancellation cannot leave a partial env file missing the instance
# name/zone needed for teardown.

{
cat <<EOF
export PROVIDER="$PROVIDER"
export OPENHOST_DOMAIN="${DOMAIN}"
export OPENHOST_PUBLIC_IP="$PUBLIC_IP"
export OPENHOST_RUN_ID="${RUN_ID}"
export OPENHOST_SSH_KEY="$SSH_KEY"
export OPENHOST_SSH_USER="host"
export E2E_HOSTED_ZONE_ID="${E2E_HOSTED_ZONE_ID:-}"
export E2E_BASE_DOMAIN="${E2E_BASE_DOMAIN:-}"
export OPENHOST_CLAIM_TOKEN="$CLAIM_TOKEN"
EOF
provider_env_vars
} > "$ENV_FILE"

# ── 3. Wait for SSH ──────────────────────────────────────────────────────

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -i $SSH_KEY"
echo "Waiting for SSH..."
wait_for_ssh "$SSH_USER" "$PUBLIC_IP" "$SSH_OPTS" 60

# ── 4. Create DNS records (TLS mode only) ────────────────────────────────

if $USE_TLS; then
    create_route53_dns "$DOMAIN" "$PUBLIC_IP" "$E2E_HOSTED_ZONE_ID"
fi

# ── 5. Deploy via ansible ────────────────────────────────────────────────

if $USE_TLS; then
    run_ansible_setup "$PUBLIC_IP" "$SSH_KEY" "$DOMAIN" "$SSH_USER"
else
    run_ansible_setup "$PUBLIC_IP" "$SSH_KEY" "" "$SSH_USER"
fi

# ── 6. Verify Cloud in a Bottle service is running ────────────────────────────────

echo ""
echo "--- Verifying Cloud in a Bottle service ---"
HOST_SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -i $SSH_KEY"
if ! ssh $HOST_SSH_OPTS "host@${PUBLIC_IP}" "systemctl is-active openhost" 2>/dev/null; then
    echo "ERROR: Cloud in a Bottle service is not running!" >&2
    ssh $HOST_SSH_OPTS "host@${PUBLIC_IP}" "sudo journalctl -u openhost --no-pager -n 50" 2>/dev/null || true
    exit 1
fi
echo "  Cloud in a Bottle service is active"

if $USE_TLS; then
    echo "Waiting for health endpoint..."
    for i in $(seq 1 60); do
        if curl -sf --max-time 5 "https://$DOMAIN/health" >/dev/null 2>&1; then
            echo "  Health check passed"
            break
        fi
        if [ "$i" -eq 60 ]; then
            echo "ERROR: Health endpoint not responding after 5 minutes" >&2
            exit 1
        fi
        sleep 5
    done
fi

# ── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "========================================"
echo "  Cloud in a Bottle is running on $PROVIDER"
echo "========================================"
if $USE_TLS; then
    echo "  URL:      https://$DOMAIN"
else
    echo "  URL:      http://$PUBLIC_IP:8080"
fi
echo "  IP:       $PUBLIC_IP"
echo "  Env file: $ENV_FILE"
echo "========================================"
