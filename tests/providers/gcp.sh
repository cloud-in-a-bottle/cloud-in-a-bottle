#!/bin/bash
# GCP provider for Cloud in a Bottle e2e tests.
#
# Implements the provider interface: provider_create, provider_teardown,
# provider_env_vars.
#
# Required environment variables:
#   GCP_PROJECT      — GCP project ID
#   GCP_ZONE         — preferred zone (default: us-central1-a)
#   GCP_MACHINE_TYPE — instance type (default: e2-standard-4)
#   GCP_IMAGE_FAMILY — OS image family (default: ubuntu-2404-lts-amd64).
#                      For arm64, use ubuntu-2404-lts-arm64 together with an
#                      arm64 machine type (e.g. GCP_MACHINE_TYPE=t2a-standard-4).
#   GCP_NETWORK_TAG  — firewall rule target tag
#   GCP_SSH_KEY      — path to SSH private key
#   GCP_SSH_USER     — SSH user (default: ubuntu)
#   GCP_DISK_SIZE    — boot disk GB (default: 30)

: "${GCP_PROJECT:?GCP_PROJECT is required}"
: "${GCP_NETWORK_TAG:?GCP_NETWORK_TAG is required}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
GCP_MACHINE_TYPE="${GCP_MACHINE_TYPE:-e2-standard-4}"
GCP_IMAGE_FAMILY="${GCP_IMAGE_FAMILY:-ubuntu-2404-lts-amd64}"
GCP_SSH_USER="${GCP_SSH_USER:-ubuntu}"
GCP_DISK_SIZE="${GCP_DISK_SIZE:-30}"

# Internal state set by provider_create
_GCP_INSTANCE_NAME=""
_GCP_ACTUAL_ZONE=""

provider_create() {
    local run_id="$1" ssh_key="$2"

    _GCP_INSTANCE_NAME="openhost-e2e-${run_id}"

    # Read public key
    local ssh_pub_key=""
    if [ -f "${ssh_key}.pub" ]; then
        ssh_pub_key=$(cat "${ssh_key}.pub")
    else
        echo "ERROR: Public key not found at ${ssh_key}.pub" >&2
        return 1
    fi

    # Build zone fallback list
    local preferred_region="${GCP_ZONE%-*}"
    local zones=("$GCP_ZONE" "${preferred_region}-b" "${preferred_region}-c" "${preferred_region}-f" "us-east1-b" "us-west1-b")
    # Deduplicate
    local -A seen
    local unique_zones=()
    for z in "${zones[@]}"; do
        if [ -z "${seen[$z]:-}" ]; then
            unique_zones+=("$z")
            seen[$z]=1
        fi
    done

    local created=false
    for zone in "${unique_zones[@]}"; do
        echo "  Trying zone: $zone" >&2
        if gcloud compute instances create "$_GCP_INSTANCE_NAME" \
            --project="$GCP_PROJECT" \
            --zone="$zone" \
            --machine-type="$GCP_MACHINE_TYPE" \
            --image-family="$GCP_IMAGE_FAMILY" \
            --image-project=ubuntu-os-cloud \
            --boot-disk-size="${GCP_DISK_SIZE}GB" \
            --boot-disk-type=pd-ssd \
            --tags="$GCP_NETWORK_TAG" \
            --metadata="ssh-keys=${GCP_SSH_USER}:${ssh_pub_key}" \
            --format=json \
            --quiet >&2 2>&1; then
            created=true
            _GCP_ACTUAL_ZONE="$zone"
            break
        else
            echo "  Zone $zone unavailable, trying next..." >&2
        fi
    done

    if ! $created; then
        echo "ERROR: Could not create instance in any zone" >&2
        return 1
    fi

    echo "  Instance: $_GCP_INSTANCE_NAME (zone: $_GCP_ACTUAL_ZONE)" >&2

    # Wait for RUNNING
    echo "  Waiting for instance to reach RUNNING state..." >&2
    for i in $(seq 1 60); do
        local status
        status=$(gcloud compute instances describe "$_GCP_INSTANCE_NAME" \
            --project="$GCP_PROJECT" \
            --zone="$_GCP_ACTUAL_ZONE" \
            --format='get(status)' 2>/dev/null || echo "UNKNOWN")
        if [ "$status" = "RUNNING" ]; then break; fi
        if [ "$i" -eq 60 ]; then
            echo "ERROR: Instance did not reach RUNNING state" >&2
            return 1
        fi
        sleep 5
    done

    # Export public IP (callers read PROVIDER_PUBLIC_IP instead of capturing stdout)
    PROVIDER_PUBLIC_IP=$(gcloud compute instances describe "$_GCP_INSTANCE_NAME" \
        --project="$GCP_PROJECT" \
        --zone="$_GCP_ACTUAL_ZONE" \
        --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
}

provider_env_vars() {
    echo "export GCP_INSTANCE_NAME=\"$_GCP_INSTANCE_NAME\""
    echo "export GCP_PROJECT=\"$GCP_PROJECT\""
    echo "export GCP_ZONE=\"$_GCP_ACTUAL_ZONE\""
}

provider_teardown() {
    local instance_name="${GCP_INSTANCE_NAME:-}"
    local project="${GCP_PROJECT:-}"
    local zone="${GCP_ZONE:-us-central1-a}"

    if [ -n "$instance_name" ] && [ -n "$project" ]; then
        echo "Deleting GCE instance $instance_name..."
        gcloud compute instances delete "$instance_name" \
            --project="$project" \
            --zone="$zone" \
            --quiet 2>/dev/null \
            && echo "  Deleted" \
            || echo "  Already deleted or not found"
    else
        echo "  No GCE instance to delete"
    fi
}
