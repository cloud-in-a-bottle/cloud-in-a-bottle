from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


class Migration0013PixiRunAsIs(SystemMigration):
    version = 13

    def up(self) -> None:
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        # The update walk installs dependencies before starting the rewritten unit.
        run("systemctl", "daemon-reload")
