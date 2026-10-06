#!/usr/bin/env bash
# on_ec2.sh: Run an image build command on an EC2 Graviton bare-metal host.
#
# GitHub's arm64 runners have no /dev/kvm, but a .metal instance does. This
# launches one, ships image/ and scripts/ from the working tree, runs <command>
# from the shipped tree, copies image/out/ back, and terminates the instance.
#
# Usage:
#   image/on_ec2.sh '<command>'
# e.g.
#   image/on_ec2.sh 'image/build.sh --branch main'
#
# Environment:
#   EC2_KEY_NAME        EC2 key pair name (required)
#   EC2_SSH_KEY         path to that key pair's private key (required)
#   EC2_SECURITY_GROUP  security group ID allowing inbound 22 (required)
#   EC2_REGION          AWS region (default: us-east-1)
#   EC2_INSTANCE_TYPES  instance types to try in order, falling through on
#                       capacity errors (default: c6g.metal c7g.metal m6g.metal)
#   EC2_MAX_MINUTES     the instance powers itself off and terminates after
#                       this long even if this script dies (default: 120)
#
# The instance is named openhost-e2e-image-*, so e2e-cleanup.yml also sweeps
# up any that leak.

set -euo pipefail

if [ $# -ne 1 ]; then
    sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "${BASH_SOURCE[0]}"
    exit 1
fi
COMMAND="$1"

: "${EC2_KEY_NAME:?EC2_KEY_NAME is required}"
: "${EC2_SSH_KEY:?EC2_SSH_KEY is required}"
: "${EC2_SECURITY_GROUP:?EC2_SECURITY_GROUP is required}"
EC2_REGION="${EC2_REGION:-us-east-1}"
EC2_INSTANCE_TYPES="${EC2_INSTANCE_TYPES:-c6g.metal c7g.metal m6g.metal}"
EC2_MAX_MINUTES="${EC2_MAX_MINUTES:-120}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_ID="$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n')"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
          -o ServerAliveInterval=30 -o ConnectTimeout=10 -i "$EC2_SSH_KEY")

AMI="$(aws ssm get-parameter --region "$EC2_REGION" \
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
    --query Parameter.Value --output text)"

# Dead man's switch: the instance terminates itself (shutdown behavior below)
# after EC2_MAX_MINUTES, so a killed CI job can't leave a metal host running.
USER_DATA="#!/bin/sh
shutdown -h +$EC2_MAX_MINUTES"

INSTANCE_ID=""
cleanup() {
    if [ -n "$INSTANCE_ID" ]; then
        echo "--- Terminating $INSTANCE_ID ---"
        aws ec2 terminate-instances --region "$EC2_REGION" --instance-ids "$INSTANCE_ID" >/dev/null \
            || echo "Warning: failed to terminate $INSTANCE_ID" >&2
    fi
}
trap cleanup EXIT

for type in $EC2_INSTANCE_TYPES; do
    echo "--- Launching $type ($AMI in $EC2_REGION) ---"
    run_args=(
        aws ec2 run-instances
        --region "$EC2_REGION"
        --image-id "$AMI"
        --instance-type "$type"
        --key-name "$EC2_KEY_NAME"
        --security-group-ids "$EC2_SECURITY_GROUP"
        --instance-initiated-shutdown-behavior terminate
        --user-data "$USER_DATA"
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=60,VolumeType=gp3}"
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=openhost-e2e-image-$RUN_ID},{Key=openhost-e2e,Value=true}]"
        --query 'Instances[0].InstanceId'
        --output text
    )
    if INSTANCE_ID="$("${run_args[@]}")"; then
        break
    fi
    INSTANCE_ID=""
done
[ -n "$INSTANCE_ID" ] || { echo "Error: could not launch any of: $EC2_INSTANCE_TYPES" >&2; exit 1; }
echo "  Instance: $INSTANCE_ID"

aws ec2 wait instance-running --region "$EC2_REGION" --instance-ids "$INSTANCE_ID"
IP="$(aws ec2 describe-instances --region "$EC2_REGION" --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
HOST="ubuntu@$IP"
remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

# Bare-metal instances take several minutes to boot.
echo "--- Waiting for SSH on $IP ---"
for i in $(seq 1 120); do
    remote true 2>/dev/null && break
    [ "$i" -eq 120 ] && { echo "Error: SSH not reachable after 20 minutes" >&2; exit 1; }
    sleep 10
done

echo "--- Installing build dependencies ---"
remote 'cloud-init status --wait >/dev/null;
    sudo apt-get update -q &&
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
        qemu-system-arm qemu-efi-aarch64 qemu-utils cloud-image-utils >/dev/null &&
    sudo chmod 666 /dev/kvm'

echo "--- Shipping image/ and scripts/ ---"
tar -C "$REPO_DIR" --exclude image/out --exclude image/cache -czf - image scripts \
    | remote 'mkdir -p repo && tar -C repo -xzf -'

echo "--- Running: $COMMAND ---"
rc=0
remote "cd repo && ($COMMAND)" < /dev/null || rc=$?

echo "--- Copying image/out back ---"
mkdir -p "$SCRIPT_DIR/out"
remote 'tar -C repo/image/out -cf - .' | tar -C "$SCRIPT_DIR/out" -xf - \
    || echo "Warning: could not copy image/out back" >&2

echo "--- Remote command exited $rc ---"
exit "$rc"
