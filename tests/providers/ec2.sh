#!/bin/bash
# EC2 provider for Cloud in a Bottle e2e tests.
#
# Implements the provider interface: provider_create, provider_teardown,
# provider_env_vars.
#
# Required environment variables:
#   EC2_SECURITY_GROUP — security group ID (must allow 22, 53, 80, 443)
#   EC2_KEY_NAME       — EC2 key pair name
#   EC2_REGION         — AWS region (default: us-east-1)
#   EC2_INSTANCE_TYPE  — instance type (default: t3.large)
#   EC2_AMI            — Ubuntu 24.04 AMI (default: us-east-1 AMI)
#   EC2_SSH_USER       — SSH user (default: ubuntu)
#   EC2_VOLUME_SIZE    — root volume GB (default: 30)
#   EC2_SUBNET_ID      — subnet (optional, uses default VPC if unset)

: "${EC2_SECURITY_GROUP:?EC2_SECURITY_GROUP is required}"
: "${EC2_KEY_NAME:?EC2_KEY_NAME is required}"
EC2_REGION="${EC2_REGION:-us-east-1}"
EC2_INSTANCE_TYPE="${EC2_INSTANCE_TYPE:-t3.large}"
EC2_AMI="${EC2_AMI:-ami-04eaa218f1349d88b}"
EC2_SSH_USER="${EC2_SSH_USER:-ubuntu}"
EC2_VOLUME_SIZE="${EC2_VOLUME_SIZE:-30}"

# Internal state set by provider_create
_EC2_INSTANCE_ID=""

provider_create() {
    local run_id="$1" ssh_key="$2"

    echo "  Launching EC2 instance ($EC2_INSTANCE_TYPE in $EC2_REGION)..." >&2

    local run_args=(
        aws ec2 run-instances
        --region "$EC2_REGION"
        --image-id "$EC2_AMI"
        --instance-type "$EC2_INSTANCE_TYPE"
        --key-name "$EC2_KEY_NAME"
        --security-group-ids "$EC2_SECURITY_GROUP"
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${EC2_VOLUME_SIZE},VolumeType=gp3}"
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=openhost-e2e-${run_id}},{Key=openhost-e2e,Value=true}]"
        --count 1
        --output json
    )
    if [ -n "${EC2_SUBNET_ID:-}" ]; then
        run_args+=(--subnet-id "$EC2_SUBNET_ID")
    fi

    local instance_json
    instance_json=$("${run_args[@]}" 2>&2)
    _EC2_INSTANCE_ID=$(echo "$instance_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['Instances'][0]['InstanceId'])")
    echo "  Instance ID: $_EC2_INSTANCE_ID" >&2

    echo "  Waiting for instance to reach 'running' state..." >&2
    aws ec2 wait instance-running \
        --region "$EC2_REGION" \
        --instance-ids "$_EC2_INSTANCE_ID"

    # Export public IP (callers read PROVIDER_PUBLIC_IP instead of capturing stdout)
    PROVIDER_PUBLIC_IP=$(aws ec2 describe-instances \
        --region "$EC2_REGION" \
        --instance-ids "$_EC2_INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' \
        --output text)
}

provider_env_vars() {
    echo "export EC2_INSTANCE_ID=\"$_EC2_INSTANCE_ID\""
    echo "export EC2_REGION=\"$EC2_REGION\""
}

provider_teardown() {
    local instance_id="${EC2_INSTANCE_ID:-}"
    local region="${EC2_REGION:-us-east-1}"

    if [ -n "$instance_id" ]; then
        echo "Terminating EC2 instance $instance_id..."
        aws ec2 terminate-instances \
            --region "$region" \
            --instance-ids "$instance_id" 2>/dev/null \
            && echo "  Terminated" \
            || echo "  Already terminated or not found"
    else
        echo "  No EC2 instance to terminate"
    fi
}
