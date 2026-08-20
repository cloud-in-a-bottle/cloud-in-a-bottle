"""Add a systemd drop-in granting the host user journal read access for OOM detection.

The memory guard detects host-level (global) OOM kills by tailing the kernel log
from the system journal (``journalctl --dmesg``), which needs membership in the
``systemd-journal`` group.  Rather than rewrite the main unit (or the shared
``build_openhost_service_unit`` builder), this grants the group through an additive
systemd drop-in — the same file ansible installs on fresh hosts, kept byte-identical
(a test enforces it).  Nothing in the baseline unit changes, so no existing
migration is touched.

This migration writes the drop-in and reloads systemd so hosts provisioned before
this change pick up the group on their next self-update.  It does NOT restart
openhost: ``daemon-reload`` makes the drop-in authoritative for the next start, and
the apply walk restarts the service at the end anyway (the group only takes effect
on a fresh process start).  Idempotent: writing the same file and reloading is safe
to repeat.
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file

JOURNAL_READ_DROPIN_PATH = "/etc/systemd/system/openhost.service.d/10-journal-read.conf"

# Additive drop-in: it layers ``SupplementaryGroups=`` onto openhost.service without
# rewriting the main unit. Kept byte-identical with the ansible copy at
# ansible/files/openhost.service.d/10-journal-read.conf (a test enforces this).
JOURNAL_READ_DROPIN = """\
# Managed by OpenHost; do not edit by hand. Kept byte-identical between
# ansible/files/openhost.service.d/10-journal-read.conf and the JOURNAL_READ_DROPIN
# constant in openhost_system_agent's v0010 migration (a test enforces this).
#
# Add the host user to the systemd-journal group so it can read the system journal.
# The memory guard tails the kernel log (via `journalctl --dmesg`) to detect
# host-level (global) OOM kills — machine ran out of RAM and reaped some process —
# which aren't scoped to any single app's cgroup and so aren't visible via podman.
# Reading kernel messages otherwise needs root or CAP_SYSLOG; this read-only group
# grants it without a kernel capability.
[Service]
SupplementaryGroups=systemd-journal
"""


class Migration0010JournalReadForOom(SystemMigration):
    version = 10

    def up(self) -> None:
        write_file(JOURNAL_READ_DROPIN_PATH, JOURNAL_READ_DROPIN, mode=0o644)
        # Make the drop-in authoritative for the next start. The apply walk restarts
        # openhost at the end, so we don't restart here.
        run("systemctl", "daemon-reload")
