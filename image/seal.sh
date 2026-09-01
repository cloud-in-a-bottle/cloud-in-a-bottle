#!/usr/bin/env bash
# seal.sh — Generalize the golden image at the end of the build boot.
#
# Runs once, as root, inside the build VM (invoked from cloud-init runcmd) after
# provisioning succeeds. It strips build-VM identity so every install is unique,
# and installs a boot-time service that grows the root filesystem to fill
# whatever disk the image is installed onto.
#
# Why a systemd service instead of cloud-init: a distributed appliance boots
# with NO cloud-init datasource (no seed ISO), so cloud-init does not run — its
# growpart/ssh-keygen modules never fire. We must do this ourselves.

set -euo pipefail

# growpart lives in cloud-guest-utils. Present on Ubuntu cloud images, but make
# sure — the boot service below depends on it. Network is up during the build.
if ! command -v growpart >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq cloud-guest-utils
fi

# ---- boot-time prepare script: grow root + ensure SSH host keys ----
install -d /usr/local/sbin
cat > /usr/local/sbin/openhost-prepare <<'PREP'
#!/usr/bin/env bash
# Grow the root filesystem to fill its disk and regenerate SSH host keys if the
# (generalized) image shipped without them. Idempotent — safe every boot.
set -uo pipefail

root_src=$(findmnt -no SOURCE / || true)          # /dev/vda1, /dev/sda1, /dev/nvme0n1p1, ...
if [ -n "${root_src:-}" ] && [ -b "$root_src" ]; then
    dev=$(basename "$root_src")                    # vda1
    disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null | head -n1)          # vda
    partnum=$(cat "/sys/class/block/$dev/partition" 2>/dev/null || true) # 1
    if [ -n "$disk" ] && [ -n "$partnum" ]; then
        # growpart grows the (last) partition into free space; resize2fs then
        # grows the mounted filesystem. Both no-op when already full.
        growpart "/dev/$disk" "$partnum" || true
        resize2fs "$root_src" || true
    fi
fi

# The generalized image ships with no SSH host keys; make a unique set on first
# boot so every install has its own server identity (and sshd can start).
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    ssh-keygen -A || true
fi
PREP
chmod 0755 /usr/local/sbin/openhost-prepare

# ---- unit: run it early, before sshd and openhost come up ----
cat > /etc/systemd/system/openhost-prepare.service <<'UNIT'
[Unit]
Description=Grow root filesystem and ensure SSH host keys
After=local-fs.target
Before=ssh.service openhost.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/openhost-prepare

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable openhost-prepare.service

# ---- strip build-VM identity ----
# Unique machine-id per install: systemd regenerates an empty file on boot
# (this path does not depend on cloud-init).
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

# Never ship the build VM's SSH host keys — openhost-prepare regenerates them.
rm -f /etc/ssh/ssh_host_*

# Drop build-time cloud-init instance data + logs. Hygiene only; cloud-init
# won't run on the distributed image anyway (no datasource).
cloud-init clean --logs --seed || true

# The embedded provisioner has done its job.
rm -f /root/provision.sh
