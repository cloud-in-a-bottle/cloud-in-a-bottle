"""Cap journald's on-disk size so the system journal can't fill the disk.

Cloud in a Bottle forwards the router process's stderr (plus forwarded Caddy and CoreDNS
output) to journald via systemd.  journald's own defaults let the journal grow
to ~10% of the filesystem (capped at 4 GB), and Cloud in a Bottle previously configured
no limit — so on long-lived hosts the journal steadily grew and, combined with
accumulated container images, could fill the disk and take the instance down.

This migration installs a journald drop-in that caps total journal size at
500 MB and requests it be applied immediately.  A drop-in under
``journald.conf.d`` is used rather than editing ``/etc/systemd/journald.conf``
so we never clobber operator or distro settings, and can cleanly manage just
this one knob.

Idempotent: writing the same drop-in and re-running ``journalctl --vacuum-size``
is safe to repeat.  Kept byte-identical with the ansible task that provisions
fresh hosts (``ansible/tasks/journald.yml``); a test enforces this.
"""

from __future__ import annotations

import subprocess

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import write_file

# Drop-in path and contents.  Shared so the ansible task and the byte-identity
# test reference the same source of truth.
JOURNALD_DROPIN_PATH = "/etc/systemd/journald.conf.d/10-openhost.conf"

# SystemMaxUse bounds the total size of the persistent journal.  500 MB is
# ample for diagnosing router/Caddy/CoreDNS issues across many restarts while
# leaving no path for the journal alone to fill a host disk.
JOURNALD_MAX_USE = "500M"

JOURNALD_DROPIN_CONTENT = (
    f"# Managed by Cloud in a Bottle; do not edit by hand.\n[Journal]\nSystemMaxUse={JOURNALD_MAX_USE}\n"
)


class Migration0006JournaldSizeCap(SystemMigration):
    version = 6

    def up(self) -> None:
        write_file(JOURNALD_DROPIN_PATH, JOURNALD_DROPIN_CONTENT, mode=0o644)
        # Reload journald so the new SystemMaxUse takes effect without a reboot.
        subprocess.run(["systemctl", "restart", "systemd-journald"], check=False)
        # Retire journal beyond the new cap right away so a host that is
        # already over 500 MB frees space now, not just at the next rotation.
        subprocess.run(
            ["journalctl", f"--vacuum-size={JOURNALD_MAX_USE}"],
            capture_output=True,
            check=False,
        )
