#!/bin/bash
# Shared helper functions for Cloud in a Bottle E2E setup and teardown scripts.
#
# Source this file from provider-specific scripts or e2e-setup.sh:
#   source "$(cd "$(dirname "$0")" && pwd)/common.sh"

# ── wait_for_ssh ─────────────────────────────────────────────────────────
# Wait until SSH is reachable on a remote host.
#
# Args:
#   $1 - SSH user
#   $2 - IP address
#   $3 - SSH options string (e.g., "-o StrictHostKeyChecking=no -i key")
#   $4 - Max attempts (each attempt waits 5s; default 60 = 5 minutes)
wait_for_ssh() {
    local user="$1" ip="$2" ssh_opts="$3" max_attempts="${4:-60}"

    for i in $(seq 1 "$max_attempts"); do
        if ssh $ssh_opts "${user}@${ip}" "true" 2>/dev/null; then
            echo "  SSH ready"
            return 0
        fi
        if [ "$i" -eq "$max_attempts" ]; then
            echo "ERROR: SSH not reachable after timeout" >&2
            exit 1
        fi
        sleep 5
    done
}

# ── create_route53_dns ───────────────────────────────────────────────────
# Create Route53 NS + A records for delegating a subdomain to Cloud in a Bottle's
# built-in DNS server.
#
# Args:
#   $1 - Domain (e.g., run123.e2e.imbue.com)
#   $2 - Public IP of the instance
#   $3 - Route53 hosted zone ID
create_route53_dns() {
    local domain="$1" public_ip="$2" hosted_zone_id="$3"

    echo ""
    echo "--- Creating Route53 DNS records ---"
    local change_batch
    change_batch=$(cat <<EOF
{
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "${domain}",
        "Type": "NS",
        "TTL": 60,
        "ResourceRecords": [
          {"Value": "ns.${domain}"}
        ]
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "ns.${domain}",
        "Type": "A",
        "TTL": 60,
        "ResourceRecords": [
          {"Value": "${public_ip}"}
        ]
      }
    }
  ]
}
EOF
    )
    local change_id
    change_id=$(aws route53 change-resource-record-sets \
        --hosted-zone-id "$hosted_zone_id" \
        --change-batch "$change_batch" \
        --query 'ChangeInfo.Id' \
        --output text)
    echo "  Route53 change: $change_id"
    echo "Waiting for DNS propagation..."
    aws route53 wait resource-record-sets-changed --id "$change_id"
    echo "  DNS records active"
}

# ── delete_route53_dns ───────────────────────────────────────────────────
# Delete Route53 NS + A records created by create_route53_dns.
# Idempotent — logs a message and continues if records are already gone.
#
# Args:
#   $1 - Domain
#   $2 - Public IP
#   $3 - Route53 hosted zone ID
delete_route53_dns() {
    local domain="$1" public_ip="$2" hosted_zone_id="$3"

    echo "Deleting DNS records for $domain..."
    local change_batch
    change_batch=$(cat <<EOF
{
  "Changes": [
    {
      "Action": "DELETE",
      "ResourceRecordSet": {
        "Name": "${domain}",
        "Type": "NS",
        "TTL": 60,
        "ResourceRecords": [
          {"Value": "ns.${domain}"}
        ]
      }
    },
    {
      "Action": "DELETE",
      "ResourceRecordSet": {
        "Name": "ns.${domain}",
        "Type": "A",
        "TTL": 60,
        "ResourceRecords": [
          {"Value": "${public_ip}"}
        ]
      }
    }
  ]
}
EOF
    )
    aws route53 change-resource-record-sets \
        --hosted-zone-id "$hosted_zone_id" \
        --change-batch "$change_batch" 2>/dev/null \
        && echo "  DNS records deleted" \
        || echo "  DNS records already deleted or not found"
}

# ── run_ansible_setup ────────────────────────────────────────────────────
# Deploy Cloud in a Bottle via ansible playbooks (no outer VM required).
# Runs ansible-playbook from the local machine targeting the remote host.
#
# Args:
#   $1 - IP address
#   $2 - SSH key path
#   $3 - Domain (empty string for HTTP-only mode)
#   $4 - SSH user on the instance (initial user, e.g., ubuntu)
#
# Environment:
#   SKIP_APT_UPGRADE=1    — skip apt dist-upgrade (speeds up ephemeral CI instances)
#   OPENHOST_COMMIT=<sha> — deploy a specific git commit (defaults to main)
#   OPENHOST_REFSPEC=<ref> — extra refspec to fetch so OPENHOST_COMMIT is reachable
#                            (e.g. refs/pull/N/merge for a PR merge commit)
run_ansible_setup() {
    local ip="$1" ssh_key="$2" domain="$3" ssh_user="${4:-ubuntu}"
    local repo_dir
    repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

    echo ""
    echo "--- Deploying Cloud in a Bottle via ansible ---"

    # Build extra vars.
    local extra_vars=()
    extra_vars+=(-e "initial_user=$ssh_user")
    extra_vars+=(-e "domain=$domain")
    extra_vars+=(-e "public_ip=$ip")
    if [ "${SKIP_APT_UPGRADE:-0}" = "1" ]; then
        extra_vars+=(-e "skip_apt_upgrade=true")
        echo "  (skipping apt dist-upgrade)"
    fi
    if [ -n "${OPENHOST_COMMIT:-}" ]; then
        extra_vars+=(-e "openhost_commit=$OPENHOST_COMMIT")
        echo "  (deploying commit $OPENHOST_COMMIT)"
    fi
    if [ -n "${OPENHOST_REFSPEC:-}" ]; then
        extra_vars+=(-e "openhost_refspec=$OPENHOST_REFSPEC")
        echo "  (fetching refspec $OPENHOST_REFSPEC)"
    fi
    if [ -n "${CLAIM_TOKEN:-}" ]; then
        extra_vars+=(-e "claim_token=$CLAIM_TOKEN")
        echo "  (claim token wired through)"
    fi

    # Run the full setup playbook.
    ANSIBLE_HOST_KEY_CHECKING=false \
    ansible-playbook "$repo_dir/ansible/setup.yml" \
        -i "${ip}," \
        --private-key "$ssh_key" \
        "${extra_vars[@]}"

    echo "  Ansible setup complete"
}
