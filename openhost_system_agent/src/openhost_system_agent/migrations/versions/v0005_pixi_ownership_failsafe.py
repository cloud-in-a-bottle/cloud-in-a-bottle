"""Reclaim host ownership and add the startup failsafe to openhost.service.

The root-run update walk (migrations, git checkout/clean, and in older versions
``pixi install``) can leave root-owned files under the host-owned Cloud in a Bottle trees
(``/home/host/openhost`` and ``/home/host/.pixi``). The host service's ``pixi
run`` then fails with EACCES and its git ops fail on root-owned objects, so it
won't start.

This migration heals such hosts and prevents recurrence:
  1. chown the trees back to host now (fixes the current host).
  2. Rewrite openhost.service to include the ``ExecStartPre=+`` reclaim step,
     so any future root-owned residue is fixed on every boot before the
     host-user ExecStart, even on hosts that can't reach an update.

Both steps are idempotent. The rewritten unit is byte-identical to the one the
baseline migration now produces (both call ``build_openhost_service_unit``).
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit
from openhost_system_agent.reclaim import reclaim_host_ownership


class Migration0005PixiOwnershipFailsafe(SystemMigration):
    version = 5

    def up(self) -> None:
        # Heal the current host up front so this migration's own downstream
        # `pixi install` (run as host by apply_after_checkout) succeeds.
        reclaim_host_ownership()

        # Install the reclaim script and wire the startup failsafe into the
        # unit so future root-owned residue self-heals on every boot. Existing
        # hosts ran the baseline before either existed, so write both here.
        write_file(RECLAIM_SCRIPT_PATH, RECLAIM_SCRIPT, mode=0o755)
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        run("systemctl", "daemon-reload")
