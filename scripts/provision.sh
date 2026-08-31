#!/usr/bin/env bash
# provision.sh — Bootstrap a fresh Ubuntu 24.04 server into a running Cloud in a Bottle instance.
#
# Usage (run as root on the target server):
#   curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh | bash -s -- --domain myhost.example.com
#
# Prerequisites:
#   - Fresh Ubuntu 24.04 server with root access
#   - DNS A record: <domain> -> server IP
#   - DNS NS + A records for subdomain delegation (see docs)
#
# What it does:
#   1. Creates the 'host' user with SSH keys from root
#   2. Installs ansible-core and git
#   3. Clones the openhost repository
#   4. Runs ansible/local_setup.yml (reuses the same tasks as remote setup.yml)
#   5. Generates an ACME account key for TLS certificates
#
# The ansible playbook handles: apt packages, podman, pixi, config, systemd service.

set -euo pipefail

DOMAIN=""
BRANCH="main"
REPO_URL="https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git"
OPENHOST_DIR="/home/host/openhost"
LOCAL_HTTP_ONLY="false"
CLAIM_TOKEN_REQUIRED="true"

usage() {
    echo "Usage: $0 --domain <domain> [--branch <branch>] [--repo <repo-url>] [--local-http-only]"
    echo "          [--no-claim-token]"
    echo ""
    echo "  --domain            Required. Domain name (e.g., myhost.example.com)."
    echo "                      In --local-http-only mode this is only used for app"
    echo "                      subdomain routing, not TLS/DNS -- include the port"
    echo "                      (e.g. lvh.me:8080), since the router builds absolute"
    echo "                      URLs from it and without it they point at :80."
    echo "  --branch            Git branch to deploy (default: main)"
    echo "  --repo              Git repo URL (default: cloud-in-a-bottle/cloud-in-a-bottle)"
    echo "  --local-http-only   HTTP-only localhost mode: no TLS, CoreDNS, or Caddy."
    echo "                      For bringing an instance up before a public domain +"
    echo "                      DNS are ready.  Reach it via an SSH tunnel to :8080."
    echo "  --no-claim-token    Leave /setup ungated, so you can claim the instance without"
    echo "                      the token.  Requires --local-http-only: on a reachable"
    echo "                      instance the token is the only thing stopping a stranger"
    echo "                      from claiming it first."
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)           DOMAIN="$2"; shift 2 ;;
        --branch)           BRANCH="$2"; shift 2 ;;
        --repo)             REPO_URL="$2"; shift 2 ;;
        --local-http-only)  LOCAL_HTTP_ONLY="true"; shift ;;
        --no-claim-token)   CLAIM_TOKEN_REQUIRED="false"; shift ;;
        -h|--help)          usage; exit 0 ;;
        *)                  echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ -z "$DOMAIN" ]; then
    echo "Error: --domain is required"
    usage
    exit 1
fi

if [ "$CLAIM_TOKEN_REQUIRED" = "false" ] && [ "$LOCAL_HTTP_ONLY" != "true" ]; then
    echo "Error: --no-claim-token requires --local-http-only"
    echo "       Without the token, anyone who can reach /setup can claim this instance."
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: this script must be run as root"
    exit 1
fi

echo "=== Cloud in a Bottle Provisioning ==="
echo "  Domain: $DOMAIN"
echo "  Branch: $BRANCH"
echo ""

# ---- Create host user ----
if ! id -u host >/dev/null 2>&1; then
    echo "--- Creating host user ---"
    useradd -m -s /bin/bash -G sudo host
    echo "host ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/host
    chmod 0440 /etc/sudoers.d/host

    # Copy SSH authorized_keys so the user can SSH in after provisioning
    if [ -f /root/.ssh/authorized_keys ]; then
        mkdir -p /home/host/.ssh
        cp /root/.ssh/authorized_keys /home/host/.ssh/authorized_keys
        chown -R host:host /home/host/.ssh
        chmod 700 /home/host/.ssh
        chmod 600 /home/host/.ssh/authorized_keys
    fi
fi

# ---- Install prerequisites ----
echo "--- Installing ansible and git ---"
apt-get update -qq
apt-get install -y -qq ansible-core git > /dev/null 2>&1

# ---- Clone the repo ----
echo "--- Cloning Cloud in a Bottle ($BRANCH) ---"
if [ -d "$OPENHOST_DIR/.git" ]; then
    cd "$OPENHOST_DIR"
    su host -c "git fetch origin"
    su host -c "git checkout $BRANCH"
    su host -c "git reset --hard origin/$BRANCH"
else
    su host -c "git clone --branch $BRANCH $REPO_URL $OPENHOST_DIR"
fi
chown -R host:host "$OPENHOST_DIR"

# ---- Detect public IP ----
PUBLIC_IP=""
PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
if [ -z "$PUBLIC_IP" ] || echo "$PUBLIC_IP" | grep -qE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)'; then
    PUBLIC_IP=$(curl -sf --max-time 5 https://ifconfig.me 2>/dev/null || true)
fi
echo "  Public IP: ${PUBLIC_IP:-unknown}"

# ---- Run ansible (but skip the service start — we need ACME key first) ----
echo "--- Running setup playbook ---"
cd "$OPENHOST_DIR"
ansible-playbook ansible/local_setup.yml \
    -e "domain=$DOMAIN" \
    -e "public_ip=${PUBLIC_IP:-127.0.0.1}" \
    -e "acme_directory_url=https://acme-v02.api.letsencrypt.org/directory" \
    -e "local_http_only=$LOCAL_HTTP_ONLY" \
    -e "claim_token_required=$CLAIM_TOKEN_REQUIRED" \
    -e "skip_service_start=true" \
    --connection=local \
    -i "localhost,"

# ---- Generate ACME account key if missing (TLS mode only) ----
if [ "$LOCAL_HTTP_ONLY" != "true" ]; then
    ACME_KEY_PATH="$OPENHOST_DIR/ansible/secrets/certbot_private_key.json"
    if [ ! -f "$ACME_KEY_PATH" ]; then
        echo "--- Generating ACME account key ---"
        mkdir -p "$(dirname "$ACME_KEY_PATH")"
        su host -c "cd $OPENHOST_DIR && /home/host/.pixi/bin/pixi run python3 scripts/generate_acme_key.py $ACME_KEY_PATH"
        chmod 600 "$ACME_KEY_PATH"
        chown host:host "$ACME_KEY_PATH"
    fi
fi

# ---- Enable + start the service ----
# `enable` here (not just `start`) because the ansible run above was invoked
# with skip_service_start=true, which now also skips enabling the unit for boot
# persistence (so warm-pool prebakes stay fully dormant). This is the point in
# the bare-metal flow where openhost is meant to come up for good, so enable it.
echo "--- Starting Cloud in a Bottle ---"
systemctl enable --now openhost

echo ""
echo "=== Cloud in a Bottle provisioning complete ==="
echo ""
if [ "$LOCAL_HTTP_ONLY" = "true" ]; then
    echo "  Mode:      HTTP-only localhost (no TLS/CoreDNS/Caddy)"
    if [ "$CLAIM_TOKEN_REQUIRED" = "false" ]; then
        echo "  Claim:     /setup is ungated (--no-claim-token); no token needed"
    fi
    echo "  Dashboard: http://localhost:8080  (SSH-tunnel to reach it:"
    echo "             ssh -L 8080:localhost:8080 host@<pi-ip>)"
else
    echo "  Dashboard: https://$DOMAIN"
    echo "  SSH:       ssh host@$DOMAIN"
fi
echo ""
echo "  Check status:  systemctl status openhost"
echo "  View logs:     journalctl -u openhost -f"
