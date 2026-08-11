"""Tests for the v7 migration that provisions a swap file on existing hosts.

The migration delegates to ``ensure_swapfile``; we assert it calls it in the
non-destructive create-only mode with the default size, and that the default
size stays in sync with the ansible task that provisions fresh hosts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from openhost_system_agent.migrations.versions.v0007_swap_file import Migration0007SwapFile
from openhost_system_agent.swap import DEFAULT_SWAP_SIZE_GIB

_PREFIX = "openhost_system_agent.migrations.versions.v0007_swap_file"


def test_provisions_default_swap_create_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_ensure(size_gib: int, *, create_only: bool = True) -> None:
        calls.append({"size_gib": size_gib, "create_only": create_only})

    monkeypatch.setattr(f"{_PREFIX}.ensure_swapfile", fake_ensure)

    Migration0007SwapFile().up()

    # Default-sized and non-destructive: an owner-resized swap file is not
    # shrunk back to the default when this migration re-runs.
    assert calls == [{"size_gib": DEFAULT_SWAP_SIZE_GIB, "create_only": True}]


def test_default_size_matches_ansible_task() -> None:
    # The migration (existing hosts) and the ansible task (fresh hosts) must
    # provision the same default swap size so a host looks the same however it
    # was set up.
    repo_root = Path(__file__).resolve().parents[4]
    task = (repo_root / "ansible" / "tasks" / "swap.yml").read_text()

    match = re.search(r"swap_size_gb\s*\|\s*default\((\d+)\)", task)
    assert match is not None, "ansible swap.yml must define a swap_size_gb default"
    assert int(match.group(1)) == DEFAULT_SWAP_SIZE_GIB
