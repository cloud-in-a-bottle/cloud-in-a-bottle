"""Rewrite openhost.service to also set BOTTLE_ROUTER_CONFIG.

The project was renamed OpenHost -> Cloud in a Bottle.  The router now reads the
config-file path from ``BOTTLE_ROUTER_CONFIG`` (preferred), falling back to the
legacy ``OPENHOST_ROUTER_CONFIG``/``OPENHOST_CONFIG``.  ``build_openhost_service_unit``
(and the ansible template) now emit both ``Environment=`` lines so the value is
available under either name; fresh hosts get it from the baseline/template, but
hosts provisioned before this change still have a unit that sets only the legacy
name on disk.

This migration rewrites the installed unit to the current
``build_openhost_service_unit`` output and reloads systemd so existing hosts pick
up the new ``Environment=BOTTLE_ROUTER_CONFIG`` on their next self-update —
without waiting for a re-provision.  It does NOT restart openhost: the legacy
name still works, so ``daemon-reload`` making the new unit authoritative for the
*next* start (the apply walk restarts the service at the end anyway) is enough.

Idempotent: writing the same unit and reloading systemd is safe to repeat.  The
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


class Migration0011BottleRouterConfigEnv(SystemMigration):
    version = 11

    def up(self) -> None:
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        # Make the rewritten unit authoritative for the next start. The apply
        # walk restarts openhost at the end, so we don't restart here.
        run("systemctl", "daemon-reload")
