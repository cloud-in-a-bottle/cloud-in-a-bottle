#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 3 || ! "$2" =~ ^[a-f0-9]{64}$ ]]; then
    echo "Usage: download_verified.sh URL SHA256 DESTINATION" >&2
    exit 2
fi
url=$1
expected=$2
destination=$3

matches_checksum() {
    local actual
    actual=$(sha256sum < "$1")
    [[ ${actual%% *} == "$expected" ]]
}

if [[ -f "$destination" ]] && matches_checksum "$destination"; then
    echo "Using verified cache: $destination"
    exit 0
fi

mkdir -p "$(dirname "$destination")"
temporary=$(mktemp "${destination}.download.XXXXXX")
trap 'rm -f "$temporary"' EXIT

# Curl retries transient HTTP errors and timeouts. The retry budget prevents a
# dependency outage from consuming the entire job; each transfer is also bounded.
curl --fail --location --silent --show-error \
    --retry 5 --retry-connrefused --retry-max-time 180 \
    --connect-timeout 15 --max-time 60 \
    --output "$temporary" --url "$url"

if ! matches_checksum "$temporary"; then
    echo "SHA256 mismatch for $destination; refusing to install." >&2
    exit 1
fi
mv -fT "$temporary" "$destination"
