from __future__ import annotations

import json
import sys
from typing import Annotated

import attr
import attrs
import cappa

from openhost_system_agent.migrations.runner import apply_system_migrations
from openhost_system_agent.status import get_migration_status
from openhost_system_agent.swap import get_swap_status
from openhost_system_agent.swap import resize_swapfile
from openhost_system_agent.update import apply_update
from openhost_system_agent.update import fetch_updates
from openhost_system_agent.update import get_remote_info
from openhost_system_agent.update import set_remote_url
from openhost_system_agent.update import show_diff


def _output(obj: object) -> None:
    print(json.dumps(attr.asdict(obj)))  # type: ignore[arg-type]


def _error(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}))
    raise SystemExit(1)


@cappa.command(name="update", help="Manage code updates and system migrations.")
@attrs.define
class UpdateCmd:
    @cappa.command(name="fetch", help="Fetch latest code from remote.")
    def fetch(self) -> None:
        try:
            _output(fetch_updates())
        except Exception as e:
            _error(str(e))

    @cappa.command(name="show-diff", help="Show pending changes between HEAD and remote.")
    def show_diff(self) -> None:
        try:
            _output(show_diff())
        except Exception as e:
            _error(str(e))

    @cappa.command(name="apply", help="Apply pending update: checkout, migrate, install deps, restart openhost.")
    def apply(self) -> None:
        # apply_update execs into the apply walk and restarts openhost on
        # success, so it never returns; only failures surface here.
        try:
            apply_update()
        except Exception as e:
            _error(str(e))

    @cappa.command(
        name="migrate", help="Apply pending system migrations for the current checkout (no fetch/checkout/restart)."
    )
    def migrate(self) -> None:
        # Decoupled from the apply walk so migrations can run at boot (openhost.service ExecStartPre)
        # or when the code is already at the target (dev branch, dirty tree) — the walk bails first.
        try:
            print(json.dumps({"ok": True, "applied": apply_system_migrations()}))
        except Exception as e:
            _error(str(e))

    @cappa.command(name="set-remote", help="Set the git remote URL.")
    def set_remote(
        self,
        url: Annotated[str, cappa.Arg(help="Git remote URL")],
    ) -> None:
        try:
            _output(set_remote_url(url))
        except Exception as e:
            _error(str(e))

    @cappa.command(name="get-remote", help="Get the current git remote URL and ref.")
    def get_remote(self) -> None:
        try:
            _output(get_remote_info())
        except Exception as e:
            _error(str(e))


@cappa.command(name="status", help="Check system migration status.")
@attrs.define
class StatusCmd:
    def __call__(self) -> None:
        _output(get_migration_status())


@cappa.command(name="swap", help="Manage the host swap file.")
@attrs.define
class SwapCmd:
    @cappa.command(name="get", help="Report the swap file's size and whether it is active.")
    def get(self) -> None:
        try:
            _output(get_swap_status())
        except Exception as e:
            _error(str(e))

    @cappa.command(name="set", help="Resize the swap file (GiB; 0 disables swap).")
    def set(
        self,
        size_gib: Annotated[int, cappa.Arg(help="Swap size in GiB (0 disables swap)")],
    ) -> None:
        try:
            _output(resize_swapfile(size_gib))
        except Exception as e:
            _error(str(e))


@cappa.command(
    name="openhost_system_agent",
    help="OpenHost system agent — host-level updates and migrations.",
)
@attrs.define
class SystemAgent:
    subcommand: cappa.Subcommands[UpdateCmd | StatusCmd | SwapCmd]


def main() -> None:
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    cappa.invoke(SystemAgent, color=False)
