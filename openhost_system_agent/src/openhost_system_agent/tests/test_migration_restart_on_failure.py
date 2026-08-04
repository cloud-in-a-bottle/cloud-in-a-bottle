"""Tests for the v8 migration that rewrites openhost.service to Restart=on-failure.

The migration exists so hosts provisioned before the restart-policy change pick
up the new unit on their next self-update, not only on a full re-provision.  It
writes the current ``build_openhost_service_unit`` output to the installed unit
path and reloads systemd; we drive it through fakes to assert exactly that.
"""

from __future__ import annotations

from typing import Any

import pytest

from openhost_system_agent.migrations.versions import v0008_restart_on_failure
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit
from openhost_system_agent.migrations.versions.v0008_restart_on_failure import Migration0008RestartOnFailure

_PREFIX = "openhost_system_agent.migrations.versions.v0008_restart_on_failure"


def test_rewrites_unit_and_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    written: dict[str, Any] = {}
    run_calls: list[tuple[str, ...]] = []

    def fake_write_file(path: str, content: str, *, mode: int = 0o600) -> None:
        written["path"] = path
        written["content"] = content
        written["mode"] = mode

    def fake_run(*cmd: str) -> None:
        run_calls.append(cmd)

    monkeypatch.setattr(f"{_PREFIX}.write_file", fake_write_file)
    monkeypatch.setattr(f"{_PREFIX}.run", fake_run)
    monkeypatch.setattr(f"{_PREFIX}.get_host_uid", lambda: 1001)

    Migration0008RestartOnFailure().up()

    # World-readable unit at the canonical path, exactly the shared builder's output.
    assert written["path"] == OPENHOST_SERVICE_PATH
    assert written["mode"] == 0o644
    assert written["content"] == build_openhost_service_unit(1001)
    assert "Restart=on-failure\n" in written["content"]

    # systemd is reloaded so the new unit is authoritative for the next start.
    assert ("systemctl", "daemon-reload") in run_calls
    # It must NOT restart openhost itself — the apply walk does that at the end;
    # a mid-migration restart would kill the running apply process.
    assert not any(c[:2] == ("systemctl", "restart") for c in run_calls)


def test_migration_version_is_eight() -> None:
    assert v0008_restart_on_failure.Migration0008RestartOnFailure.version == 8
