#!/bin/bash
# Hetzner Cloud provider for Cloud in a Bottle e2e tests.
#
# Implements the provider interface: provider_create, provider_teardown,
# provider_env_vars.
#
# Required environment variables:
#   HETZNER_TOKEN       — Hetzner Cloud API token (read/write)
#   HETZNER_LOCATION    — preferred location (default: ash)
#   HETZNER_SERVER_TYPE — server type (default: cpx32)
#   E2E_SSH_KEY         — path to SSH private key (used by e2e-setup.sh)

: "${HETZNER_TOKEN:?HETZNER_TOKEN is required}"
HETZNER_LOCATION="${HETZNER_LOCATION:-ash}"
HETZNER_SERVER_TYPE="${HETZNER_SERVER_TYPE:-cpx32}"

HETZNER_API="https://api.hetzner.cloud/v1"

# Internal state set by provider_create
_HETZNER_SERVER_ID=""
_HETZNER_SERVER_NAME=""
_HETZNER_SSH_KEY_ID=""

# Helper: call the Hetzner API and return the JSON response.
# Exits with an error if the response is empty or contains an API error.
_hetzner_api() {
    local response
    response=$(curl -s -H "Authorization: Bearer $HETZNER_TOKEN" \
        -H "Content-Type: application/json" "$@") || {
        echo "ERROR: curl failed (exit $?)" >&2
        return 1
    }
    if [ -z "$response" ]; then
        echo "ERROR: empty API response" >&2
        return 1
    fi
    # Check for API-level error
    local err
    err=$(echo "$response" | python3 -c "
import sys, json
r = json.load(sys.stdin)
e = r.get('error', {})
if e:
    print(f\"{e.get('code','unknown')}: {e.get('message','')}\")
" 2>/dev/null || true)
    if [ -n "$err" ]; then
        echo "ERROR: Hetzner API: $err" >&2
        return 1
    fi
    echo "$response"
}

provider_create() {
    local run_id="$1" ssh_key="$2"

    _HETZNER_SERVER_NAME="openhost-e2e-${run_id}"

    # Read public key
    local ssh_pub_key=""
    if [ -f "${ssh_key}.pub" ]; then
        ssh_pub_key=$(cat "${ssh_key}.pub")
    else
        echo "ERROR: Public key not found at ${ssh_key}.pub" >&2
        return 1
    fi

    # Upload SSH key to Hetzner (or reuse existing)
    local key_name="${_HETZNER_SERVER_NAME}-key"
    echo "  Uploading SSH key as '$key_name'..." >&2

    # Check for existing key with this name
    local existing_key=""
    local list_response
    if list_response=$(_hetzner_api "${HETZNER_API}/ssh_keys?name=${key_name}"); then
        existing_key=$(echo "$list_response" | python3 -c "
import sys, json
keys = json.load(sys.stdin)['ssh_keys']
print(keys[0]['id'] if keys else '')
")
    fi

    if [ -n "$existing_key" ]; then
        _HETZNER_SSH_KEY_ID="$existing_key"
        echo "  Reusing SSH key: $_HETZNER_SSH_KEY_ID" >&2
    else
        # Build JSON payload safely using python to handle key encoding
        local payload
        payload=$(python3 -c "
import json, sys
print(json.dumps({'name': sys.argv[1], 'public_key': sys.argv[2]}))
" "$key_name" "$ssh_pub_key")

        local create_key_response
        create_key_response=$(_hetzner_api -X POST "${HETZNER_API}/ssh_keys" -d "$payload")
        _HETZNER_SSH_KEY_ID=$(echo "$create_key_response" | python3 -c "import sys,json; print(json.load(sys.stdin)['ssh_key']['id'])")
        echo "  Created SSH key: $_HETZNER_SSH_KEY_ID" >&2
    fi

    # Build location fallback list
    local locations=("$HETZNER_LOCATION" "ash" "hil" "fsn1" "nbg1" "hel1")
    local -A seen
    local unique_locations=()
    for loc in "${locations[@]}"; do
        if [ -z "${seen[$loc]:-}" ]; then
            unique_locations+=("$loc")
            seen[$loc]=1
        fi
    done

    local created=false
    local create_response
    for location in "${unique_locations[@]}"; do
        echo "  Trying location: $location" >&2

        # Build server creation payload safely
        local server_payload
        server_payload=$(python3 -c "
import json, sys
print(json.dumps({
    'name': sys.argv[1],
    'server_type': sys.argv[2],
    'image': 'ubuntu-24.04',
    'location': sys.argv[3],
    'ssh_keys': [int(sys.argv[4])],
    'public_net': {'enable_ipv4': True, 'enable_ipv6': True},
    'labels': {'managed-by': 'openhost-e2e'}
}))
" "$_HETZNER_SERVER_NAME" "$HETZNER_SERVER_TYPE" "$location" "$_HETZNER_SSH_KEY_ID")

        if create_response=$(_hetzner_api -X POST "${HETZNER_API}/servers" -d "$server_payload"); then
            created=true
            HETZNER_LOCATION="$location"
            break
        else
            echo "  Location $location unavailable, trying next..." >&2
        fi
    done

    if ! $created; then
        echo "ERROR: Could not create server in any location" >&2
        return 1
    fi

    _HETZNER_SERVER_ID=$(echo "$create_response" | python3 -c "import sys,json; print(json.load(sys.stdin)['server']['id'])")
    echo "  Server: $_HETZNER_SERVER_NAME (id: $_HETZNER_SERVER_ID, location: $HETZNER_LOCATION)" >&2

    # Wait for server to reach 'running' status
    echo "  Waiting for server to reach 'running' state..." >&2
    for i in $(seq 1 60); do
        local status
        status=$(curl -sf -H "Authorization: Bearer $HETZNER_TOKEN" \
            "${HETZNER_API}/servers/$_HETZNER_SERVER_ID" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['server']['status'])" 2>/dev/null || echo "unknown")
        if [ "$status" = "running" ]; then break; fi
        if [ "$i" -eq 60 ]; then
            echo "ERROR: Server did not reach 'running' state (status: $status)" >&2
            return 1
        fi
        sleep 5
    done

    # Export public IP
    PROVIDER_PUBLIC_IP=$(curl -sf -H "Authorization: Bearer $HETZNER_TOKEN" \
        "${HETZNER_API}/servers/$_HETZNER_SERVER_ID" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['server']['public_net']['ipv4']['ip'])")
}

provider_env_vars() {
    echo "export HETZNER_SERVER_ID=\"$_HETZNER_SERVER_ID\""
    echo "export HETZNER_SERVER_NAME=\"$_HETZNER_SERVER_NAME\""
    echo "export HETZNER_SSH_KEY_ID=\"$_HETZNER_SSH_KEY_ID\""
    echo "export HETZNER_TOKEN=\"$HETZNER_TOKEN\""
}

provider_teardown() {
    local server_id="${HETZNER_SERVER_ID:-}"
    local ssh_key_id="${HETZNER_SSH_KEY_ID:-}"
    local token="${HETZNER_TOKEN:-}"

    if [ -n "$server_id" ] && [ -n "$token" ]; then
        echo "Deleting Hetzner server $server_id..."
        curl -sf -X DELETE -H "Authorization: Bearer $token" \
            "${HETZNER_API}/servers/$server_id" 2>/dev/null \
            && echo "  Deleted" \
            || echo "  Already deleted or not found"
    else
        echo "  No Hetzner server to delete"
    fi

    if [ -n "$ssh_key_id" ] && [ -n "$token" ]; then
        echo "Deleting Hetzner SSH key $ssh_key_id..."
        curl -sf -X DELETE -H "Authorization: Bearer $token" \
            "${HETZNER_API}/ssh_keys/$ssh_key_id" 2>/dev/null \
            && echo "  SSH key deleted" \
            || echo "  SSH key already deleted or not found"
    fi
}
