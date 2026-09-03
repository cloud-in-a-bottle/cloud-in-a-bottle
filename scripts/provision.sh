#!/usr/bin/env bash
# provision.sh — Bootstrap a fresh Ubuntu 24.04 server into a running Cloud in a Bottle instance.
#
# Usage (run as root on the target server):
#   curl -fsSL https://raw.githubusercontent.com/cloud-in-a-bottle/cloud-in-a-bottle/main/scripts/provision.sh | sudo bash -s -- --domain myhost.example.com
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
BIND_HOST=""
CLAIM_TOKEN=""
SWAP_SIZE_GB=""
OPEN_CLAIM="false"
PUBLIC_IP_OVERRIDE=""
ACME_KEY_SRC=""
ACME_EMAIL=""

usage() {
    echo "Usage: $0 --domain <domain> [--branch <branch>] [--repo <repo-url>] [--local-http-only]"
    echo "          [--bind-host <addr>] [--claim-token <token>] [--swap-size <gb>] [--open-claim]"
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
    echo "  --bind-host         Router bind address (default: config default,"
    echo "                      127.0.0.1). Pass 0.0.0.0 to reach :8080 over the LAN"
    echo "                      (used by the VM image build so the dashboard is"
    echo "                      reachable from the host machine)."
    echo "  --claim-token       Fixed claim token to bake in (default: random,"
    echo "                      printed at the end). Set a known value for a"
    echo "                      distributable image with a predictable claim URL."
    echo "  --swap-size         Swap file size in GiB (default: playbook default,"
    echo "                      16). Smaller values suit constrained local VMs."
    echo "  --public-ip         Public IPv4 to bake into the config for DNS records,"
    echo "                      overriding auto-detection. Use when the box running"
    echo "                      provision.sh isn't the box that'll serve the domain"
    echo "                      (e.g. building a distributable image)."
    echo "  --acme-key          Path to a pre-registered ACME account key to install"
    echo "                      instead of generating one (TLS mode only)."
    echo "  --acme-email        Email for the generated ACME account (TLS mode, when"
    echo "                      no --acme-key is given)."
    echo "  --open-claim        Leave /setup ungated (claim_token_required = false), so you"
    echo "                      can claim the instance without a token. For a private,"
    echo "                      unexposed instance (e.g. the distributed VM image behind"
    echo "                      NAT) where a shipped default token would be a public"
    echo "                      non-secret. Requires --local-http-only: on a reachable"
    echo "                      instance the token is the only thing stopping a stranger"
    echo "                      from claiming it first. Re-enable via config if you later"
    echo "                      expose the instance on a network."
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)           DOMAIN="$2"; shift 2 ;;
        --branch)           BRANCH="$2"; shift 2 ;;
        --repo)             REPO_URL="$2"; shift 2 ;;
        --local-http-only)  LOCAL_HTTP_ONLY="true"; shift ;;
        --bind-host)        BIND_HOST="$2"; shift 2 ;;
        --claim-token)      CLAIM_TOKEN="$2"; shift 2 ;;
        --swap-size)        SWAP_SIZE_GB="$2"; shift 2 ;;
        --public-ip)        PUBLIC_IP_OVERRIDE="$2"; shift 2 ;;
        --acme-key)         ACME_KEY_SRC="$2"; shift 2 ;;
        --acme-email)       ACME_EMAIL="$2"; shift 2 ;;
        --open-claim)       OPEN_CLAIM="true"; shift ;;
        -h|--help)          usage; exit 0 ;;
        *)                  echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ -z "$DOMAIN" ]; then
    echo "Error: --domain is required"
    usage
    exit 1
fi

if [ "$OPEN_CLAIM" = "true" ] && [ "$LOCAL_HTTP_ONLY" != "true" ]; then
    echo "Error: --open-claim requires --local-http-only"
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
su host -c "git config --global http.version HTTP/1.1"

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

# ---- Public IP (explicit override, else auto-detect) ----
PUBLIC_IP="$PUBLIC_IP_OVERRIDE"
if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    if [ -z "$PUBLIC_IP" ] || echo "$PUBLIC_IP" | grep -qE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)'; then
        PUBLIC_IP=$(curl -sf --max-time 5 https://ifconfig.me 2>/dev/null || true)
    fi
fi
echo "  Public IP: ${PUBLIC_IP:-unknown}"

# ---- Run ansible (but skip the service start — we need ACME key first) ----
echo "--- Running setup playbook ---"
cd "$OPENHOST_DIR"

# Optional passthrough vars. Only add the -e when set, so we don't override the
# config template's defaults (e.g. bind_host defaults to 127.0.0.1) with empties.
EXTRA_VARS=()
if [ -n "$BIND_HOST" ]; then
    EXTRA_VARS+=(-e "bind_host=$BIND_HOST")
fi
if [ -n "$CLAIM_TOKEN" ]; then
    EXTRA_VARS+=(-e "claim_token=$CLAIM_TOKEN")
fi
if [ -n "$SWAP_SIZE_GB" ]; then
    EXTRA_VARS+=(-e "swap_size_gb=$SWAP_SIZE_GB")
fi
if [ "$OPEN_CLAIM" = "true" ]; then
    EXTRA_VARS+=(-e "claim_token_required=false")
fi

ansible-playbook ansible/local_setup.yml \
    -e "domain=$DOMAIN" \
    -e "public_ip=${PUBLIC_IP:-127.0.0.1}" \
    -e "acme_directory_url=https://acme-v02.api.letsencrypt.org/directory" \
    -e "local_http_only=$LOCAL_HTTP_ONLY" \
    -e "skip_service_start=true" \
    "${EXTRA_VARS[@]}" \
    --connection=local \
    -i "localhost,"

# ---- ACME account key (TLS mode only): install the provided one, else generate ----
if [ "$LOCAL_HTTP_ONLY" != "true" ]; then
    ACME_KEY_PATH="$OPENHOST_DIR/ansible/secrets/certbot_private_key.json"
    ACME_KEY_DIR="$(dirname "$ACME_KEY_PATH")"
    if [ -n "$ACME_KEY_SRC" ]; then
        echo "--- Installing provided ACME account key ---"
        mkdir -p "$ACME_KEY_DIR"
        cp "$ACME_KEY_SRC" "$ACME_KEY_PATH"
        chmod 600 "$ACME_KEY_PATH"
        chown host:host "$ACME_KEY_PATH"
    elif [ ! -f "$ACME_KEY_PATH" ]; then
        echo "--- Generating ACME account key ---"
        mkdir -p "$ACME_KEY_DIR"
        chown host:host "$ACME_KEY_DIR"
        gen="/home/host/.pixi/bin/pixi run python3 scripts/generate_acme_key.py $ACME_KEY_PATH"
        [ -n "$ACME_EMAIL" ] && gen="$gen --email $ACME_EMAIL"
        su host -c "cd $OPENHOST_DIR && $gen"
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
    if [ "$OPEN_CLAIM" = "true" ]; then
        echo "  Claim:     /setup is ungated (--open-claim); no token needed"
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
