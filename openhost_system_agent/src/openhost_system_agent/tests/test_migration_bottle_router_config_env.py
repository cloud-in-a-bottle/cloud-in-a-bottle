"""Tests for the v11 migration that rewrites openhost.service to also set
BOTTLE_ROUTER_CONFIG (OpenHost -> Cloud in a Bottle env-var rename).

The migration exists so hosts provisioned before the rename pick up the new
``Environment=BOTTLE_ROUTER_CONFIG`` on their next self-update, not only on a
full re-provision.  It writes the current ``build_openhost_service_unit`` output
to the installed unit path and reloads systemd; we drive it through fakes to
assert exactly that.
"""

from __future__ import annotations

from typing import Any

import pytest

from openhost_system_agent.migrations.versions import v0011_bottle_router_config_env
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit
from openhost_system_agent.migrations.versions.v0011_bottle_router_config_env import Migration0011BottleRouterConfigEnv

_PREFIX = "openhost_system_agent.migrations.versions.v0011_bottle_router_config_env"


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

    Migration0011BottleRouterConfigEnv().up()

    # World-readable unit at the canonical path, exactly the shared builder's output.
    assert written["path"] == OPENHOST_SERVICE_PATH
    assert written["mode"] == 0o644
    assert written["content"] == build_openhost_service_unit(1001)
    # Both the new and legacy config-path env vars are present.
    assert "Environment=BOTTLE_ROUTER_CONFIG=" in written["content"]
    assert "Environment=OPENHOST_ROUTER_CONFIG=" in written["content"]

    # systemd is reloaded so the new unit is authoritative for the next start.
    assert ("systemctl", "daemon-reload") in run_calls
    # It must NOT restart openhost itself — the apply walk does that at the end;
    # a mid-migration restart would kill the running apply process.
    assert not any(c[:2] == ("systemctl", "restart") for c in run_calls)


def test_migration_version_is_eleven() -> None:
    assert v0011_bottle_router_config_env.Migration0011BottleRouterConfigEnv.version == 11
