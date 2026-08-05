"""Rewrite openhost.service to auto-restart on failure (was Restart=no).

compute_space is the parent of the in-process CoreDNS + Caddy children, so when
it exits they exit with it and the instance loses authoritative DNS *and*
HTTP/S at once — including its own nameserver, so nothing about the box
resolves.  With the old ``Restart=no`` a transient crash left the instance dark
until a human rebooted.  ``build_openhost_service_unit`` now emits
``Restart=on-failure`` with a bounded ``StartLimitBurst``/``StartLimitIntervalSec``
crash limiter; fresh hosts get it from the baseline and the ansible template,
but hosts provisioned before this change still have the old unit on disk.

This migration rewrites the installed unit to the current
``build_openhost_service_unit`` output and reloads systemd so existing hosts
pick up the new policy on their next self-update — without waiting for a full
re-provision.  It does NOT restart openhost: ``daemon-reload`` makes the new
policy authoritative for the *next* start, and the update flow restarts the
service at the end of the apply walk anyway (see apply_after_checkout.main).

Idempotent: writing the same unit and reloading systemd is safe to repeat.  The
written unit is byte-identical to what the baseline migration and the ansible
template produce (all three go through ``build_openhost_service_unit`` / the
matching template), so a host looks the same however it was set up.
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


class Migration0008RestartOnFailure(SystemMigration):
    version = 8

    def up(self) -> None:
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        # Make the rewritten unit authoritative for the next start. The apply
        # walk restarts openhost at the end, so we don't restart here.
        run("systemctl", "daemon-reload")
