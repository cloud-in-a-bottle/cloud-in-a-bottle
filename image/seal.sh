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
#
# --raspi: the image is the Raspberry Pi one (build.sh --raspi). There cloud-init
# does run on first boot (it reads Raspberry Pi Imager's settings from the boot
# partition), and the instance takes its .local domain from the hostname.

set -euo pipefail

RASPI="false"
[ "${1:-}" = "--raspi" ] && RASPI="true"

# growpart lives in cloud-guest-utils. Present on Ubuntu cloud images, but make
# sure — the boot service below depends on it. Network is up during the build.
if ! command -v growpart >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq cloud-guest-utils
fi

# ---- boot-time prepare script: grow root + ensure SSH host keys ----
install -d /usr/local/sbin
cat > /usr/local/sbin/bottle-prepare <<'PREP'
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
chmod 0755 /usr/local/sbin/bottle-prepare

# ---- unit: run it early, before sshd and the app come up ----
# NOTE: the app unit is still named openhost.service (ansible/templates/), so the
# Before= below deliberately keeps the old name.
cat > /etc/systemd/system/bottle-prepare.service <<'UNIT'
[Unit]
Description=Grow root filesystem and ensure SSH host keys
After=local-fs.target
Before=ssh.service openhost.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/bottle-prepare

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable bottle-prepare.service

# ---- strip build-VM identity ----
# Unique machine-id per install: systemd regenerates an empty file on boot
# (this path does not depend on cloud-init).
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

# Never ship the build VM's SSH host keys; bottle-prepare regenerates them.
rm -f /etc/ssh/ssh_host_*

# Drop build-time cloud-init instance data + logs. Hygiene only; cloud-init
# won't run on the distributed image anyway (no datasource).
cloud-init clean --logs --seed || true

# The embedded provisioner has done its job.
rm -f /root/provision.sh /root/pre-provision.sh

if [ "$RASPI" = "true" ]; then
    # Set by image/raspi/pre-provision.sh for the build VM; a real Pi identifies itself.
    rm -f /etc/flash-kernel/machine

    # The domain baked in at build time is a placeholder: on first boot, publish the
    # instance at the hostname cloud-init just set (from Raspberry Pi Imager, or
    # the image's default), so the name the user picked is the address they visit.
    # first_boot.toml is read once, when the router first starts, so this must run
    # before it.
    cat > /usr/local/sbin/bottle-raspi-domain <<'DOMAIN'
#!/usr/bin/env bash
set -euo pipefail
name=$(hostname -s | tr '[:upper:]' '[:lower:]')
sed -i "s/^domain = .*/domain = \"$name.local\"/" /home/host/.openhost/local_compute_space/first_boot.toml
echo "bottle-raspi-domain: serving at http://$name.local"
touch /var/lib/bottle-raspi-domain.done
DOMAIN
    chmod 0755 /usr/local/sbin/bottle-raspi-domain
    # Runs once, guarded by its own marker (not ConditionFirstBoot: systemd
    # doesn't count the empty machine-id the image ships with as a first boot).
    # cloud-init.service / cloud-init-network.service: whichever this cloud-init
    # version uses for the stage that sets the hostname.
    cat > /etc/systemd/system/bottle-raspi-domain.service <<'UNIT'
[Unit]
Description=Use the hostname as the Cloud in a Bottle .local domain
ConditionPathExists=!/var/lib/bottle-raspi-domain.done
After=cloud-init.service cloud-init-network.service
Before=openhost.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/bottle-raspi-domain

[Install]
WantedBy=multi-user.target
UNIT
    systemctl enable bottle-raspi-domain.service

    # Report how much of the disk provisioning used (sizes the image's --disk-size),
    # then discard free blocks so the build's raw disk stays sparse and the
    # compressed image small.
    df -h /
    fstrim -av || true
fi
