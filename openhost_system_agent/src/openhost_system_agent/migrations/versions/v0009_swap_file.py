"""Provision a swap file on existing hosts.

Small instances run out of RAM under memory pressure and get apps OOM-killed. A
swap file gives the kernel somewhere to spill cold pages. Fresh hosts get one
from ``ansible/tasks/swap.yml``; this migration applies the same default-sized
swap file to hosts provisioned before swap existed.

Idempotent and non-destructive: ``ensure_swapfile(create_only=True)`` creates
the default-sized file only when the host has none, so a host whose owner later
resized swap (e.g. ``sudo openhost_system_agent swap set 32``) is never shrunk
back to the default when this migration re-runs.
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.swap import DEFAULT_SWAP_SIZE_GIB
from openhost_system_agent.swap import ensure_swapfile


class Migration0009SwapFile(SystemMigration):
    version = 9

    def up(self) -> None:
        ensure_swapfile(DEFAULT_SWAP_SIZE_GIB, create_only=True)
