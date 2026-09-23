from __future__ import annotations

from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


class Migration0014SelectiveOwnershipRepair(SystemMigration):
    version = 14

    def up(self) -> None:
        write_file(RECLAIM_SCRIPT_PATH, RECLAIM_SCRIPT, mode=0o755)
        Path(RECLAIM_SCRIPT_PATH).chmod(0o755)
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        Path(OPENHOST_SERVICE_PATH).chmod(0o644)
        # The update walk prepares dependencies before starting the service.
        run("systemctl", "daemon-reload")
