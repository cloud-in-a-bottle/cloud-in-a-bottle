"""Tests for the swap-file management module.

Real mkswap/swapon and /etc/fstab edits need root and a real host, so we drive
the logic through fakes: a recording ``subprocess.run`` and a no-op ``chmod``,
with SWAP_PATH / _FSTAB_PATH pointed at tmp files.  This asserts the command
sequence, the fallback path, the create-only guard, and fstab idempotency
without touching the host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import openhost_system_agent.swap as swap
from openhost_system_agent.protocol import SwapStatus


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """Run swap ops as fake-root with recorded commands and a tmp swap path.

    Returns the list that accumulates each ``subprocess.run`` argv so tests can
    assert on the command sequence.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swap, "SWAP_PATH", str(tmp_path / "swapfile"))
    monkeypatch.setattr("openhost_system_agent.swap.os.geteuid", lambda: 0)
    monkeypatch.setattr("openhost_system_agent.swap.os.chmod", lambda *a, **k: None)
    monkeypatch.setattr("openhost_system_agent.swap.subprocess.run", fake_run)
    return calls


def _cmds(calls: list[list[str]]) -> list[str]:
    return [c[0] for c in calls]


def test_requires_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.swap.os.geteuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="must be run as root"):
        swap.resize_swapfile(8)


def test_resize_rejects_out_of_range(fake_host: list[list[str]]) -> None:
    with pytest.raises(ValueError, match="between"):
        swap.resize_swapfile(swap.MAX_SWAP_SIZE_GIB + 1)
    # Nothing should have run before validation failed.
    assert fake_host == []


def test_resize_creates_and_persists(fake_host: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    fstab: dict[str, bool] = {}
    monkeypatch.setattr(swap, "_set_fstab_entry", lambda present: fstab.update(present=present))
    monkeypatch.setattr(
        swap, "get_swap_status", lambda: SwapStatus(size_bytes=8 * 1024**3, path=swap.SWAP_PATH, active=True)
    )

    result = swap.resize_swapfile(8)

    assert "fallocate" in _cmds(fake_host)
    assert "mkswap" in _cmds(fake_host)
    assert "swapon" in _cmds(fake_host)
    assert fstab == {"present": True}
    assert result.active is True


def test_resize_zero_disables_and_removes(fake_host: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    fstab: dict[str, bool] = {}
    monkeypatch.setattr(swap, "_set_fstab_entry", lambda present: fstab.update(present=present))
    monkeypatch.setattr(swap, "get_swap_status", lambda: SwapStatus(size_bytes=0, path=swap.SWAP_PATH, active=False))

    # A leftover swap file should be removed.
    Path(swap.SWAP_PATH).write_text("stale")

    swap.resize_swapfile(0)

    assert _cmds(fake_host) == ["swapoff"]
    assert not Path(swap.SWAP_PATH).exists()
    assert fstab == {"present": False}


def test_create_falls_back_to_dd_when_fallocate_unswappable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # First swapon attempt (fallocate path) fails; the dd path must then run.
    calls: list[list[str]] = []
    seen_swapon = {"n": 0}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "swapon":
            seen_swapon["n"] += 1
            if seen_swapon["n"] == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "swapon: invalid argument")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swap, "SWAP_PATH", str(tmp_path / "swapfile"))
    monkeypatch.setattr("openhost_system_agent.swap.os.geteuid", lambda: 0)
    monkeypatch.setattr("openhost_system_agent.swap.os.chmod", lambda *a, **k: None)
    monkeypatch.setattr("openhost_system_agent.swap.subprocess.run", fake_run)

    swap._create_and_enable(8)

    assert "dd" in _cmds(calls)
    # swapon attempted twice: once on the fallocate path, once after dd.
    assert seen_swapon["n"] == 2


def test_ensure_create_only_keeps_existing_size(fake_host: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    # An existing swap file must not be recreated (which would resize it).
    Path(swap.SWAP_PATH).write_text("existing swap")

    def boom(size_gib: int) -> SwapStatus:
        raise AssertionError("resize must not run when a swap file already exists")

    monkeypatch.setattr(swap, "resize_swapfile", boom)
    monkeypatch.setattr(swap, "_set_fstab_entry", lambda present: None)
    monkeypatch.setattr(swap, "get_swap_status", lambda: SwapStatus(size_bytes=99, path=swap.SWAP_PATH, active=True))

    swap.ensure_swapfile(swap.DEFAULT_SWAP_SIZE_GIB, create_only=True)

    # It re-enables the present file rather than recreating it.
    assert _cmds(fake_host) == ["swapon"]


def test_ensure_creates_when_absent(fake_host: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, int | None] = {"size": None}

    def fake_resize(size_gib: int) -> SwapStatus:
        called["size"] = size_gib
        return SwapStatus(size_bytes=size_gib * 1024**3, path=swap.SWAP_PATH, active=True)

    monkeypatch.setattr(swap, "resize_swapfile", fake_resize)

    swap.ensure_swapfile(swap.DEFAULT_SWAP_SIZE_GIB, create_only=True)

    assert called["size"] == swap.DEFAULT_SWAP_SIZE_GIB


def test_set_fstab_entry_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("UUID=abc / ext4 defaults 0 1\n")
    monkeypatch.setattr(swap, "_FSTAB_PATH", str(fstab))

    swap._set_fstab_entry(present=True)
    swap._set_fstab_entry(present=True)
    text = fstab.read_text()

    # Operator's root entry survives; our line appears exactly once.
    assert "UUID=abc / ext4 defaults 0 1" in text
    assert text.count(swap._FSTAB_LINE) == 1

    swap._set_fstab_entry(present=False)
    assert swap._FSTAB_LINE not in fstab.read_text()
    assert "UUID=abc / ext4 defaults 0 1" in fstab.read_text()


def test_get_swap_status_reports_file_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    swapfile = tmp_path / "swapfile"
    swapfile.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(swap, "SWAP_PATH", str(swapfile))
    monkeypatch.setattr(swap, "_active_swap_bytes", lambda: 0)

    status = swap.get_swap_status()

    assert status.size_bytes == 4096
    assert status.path == str(swapfile)
    assert status.active is False
