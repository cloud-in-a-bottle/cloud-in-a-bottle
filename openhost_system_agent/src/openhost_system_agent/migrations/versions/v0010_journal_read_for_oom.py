"""Rewrite openhost.service to grant the host user journal read access for OOM detection.

The memory guard detects host-level (global) OOM kills by tailing the kernel log.
It now reads that from the system journal (``journalctl --dmesg``) instead of
``/dev/kmsg`` directly, so the unit no longer needs the CAP_SYSLOG capability —
``build_openhost_service_unit`` grants the host user the ``systemd-journal`` group
via ``SupplementaryGroups=`` (read-only journal access, no kernel capability).
Fresh hosts get this from the baseline and the ansible template, but hosts
provisioned before this change still have the old unit on disk.

This migration rewrites the installed unit to the current
``build_openhost_service_unit`` output and reloads systemd so existing hosts pick
up the group on their next self-update — without a full re-provision. It does NOT
restart openhost: ``daemon-reload`` makes the new unit authoritative for the next
start, and the apply walk restarts the service at the end anyway (the group only
takes effect on a fresh process start). See apply_after_checkout.main.

Idempotent: writing the same unit and reloading systemd is safe to repeat. The
written unit is byte-identical to what the baseline migration and the ansible
template produce (all go through ``build_openhost_service_unit`` / the matching
template), so a host looks the same however it was set up.
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


class Migration0010JournalReadForOom(SystemMigration):
    version = 10

    def up(self) -> None:
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        # Make the rewritten unit authoritative for the next start. The apply walk
        # restarts openhost at the end, so we don't restart here.
        run("systemctl", "daemon-reload")
