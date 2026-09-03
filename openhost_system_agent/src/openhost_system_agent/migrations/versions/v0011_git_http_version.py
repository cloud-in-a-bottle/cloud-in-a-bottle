from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run


class Migration0011GitHttpVersion(SystemMigration):
    version = 11

    def up(self) -> None:
        # Self-update fetches run as root.
        run(
            "sudo",
            "-u",
            "root",
            "-H",
            "git",
            "config",
            "--global",
            "--replace-all",
            "http.version",
            "HTTP/1.1",
        )

        # Migrations run as root; -H makes --global target /home/host/.gitconfig.
        run(
            "sudo",
            "-u",
            "host",
            "-H",
            "git",
            "config",
            "--global",
            "--replace-all",
            "http.version",
            "HTTP/1.1",
        )
