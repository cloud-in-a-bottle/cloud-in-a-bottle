#!/usr/bin/env bash
# Runs in the --raspi build VM before provision.sh.
#
# The build boots the Pi image's own kernel on a generic QEMU machine, so flash-kernel (which
# copies kernels, initrds and device trees onto the Pi's FAT boot partition whenever a kernel or
# initramfs is updated) can't recognise the board and would fail every apt upgrade that touches
# them. Name a Pi model for it. Its Pi method installs every model's device tree, not just this
# one's, so the result boots on any supported Pi. seal.sh --raspi removes the override.
set -euo pipefail
mkdir -p /etc/flash-kernel
echo "Raspberry Pi 4 Model B" > /etc/flash-kernel/machine
