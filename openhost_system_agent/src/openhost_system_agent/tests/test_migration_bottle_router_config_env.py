"""Tests for the v11 migration that advertises the router config path under the
new BOTTLE_ROUTER_CONFIG name (OpenHost -> Cloud in a Bottle env-var rename).

The migration installs an additive systemd drop-in — it does NOT rewrite the main
unit or the shared ``build_openhost_service_unit`` builder (which lives in the
v0002 baseline migration and must not change).  So hosts provisioned before the
rename expose ``BOTTLE_ROUTER_CONFIG`` on their next self-update; we drive the
migration through fakes to assert exactly that.
"""

from __future__ import annotations

from typing import Any

import pytest

from openhost_system_agent.migrations.versions import v0011_bottle_router_config_env
from openhost_system_agent.migrations.versions.v0011_bottle_router_config_env import BOTTLE_ROUTER_CONFIG_DROPIN
from openhost_system_agent.migrations.versions.v0011_bottle_router_config_env import BOTTLE_ROUTER_CONFIG_DROPIN_PATH
from openhost_system_agent.migrations.versions.v0011_bottle_router_config_env import Migration0011BottleRouterConfigEnv

_PREFIX = "openhost_system_agent.migrations.versions.v0011_bottle_router_config_env"


def test_writes_additive_dropin_and_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
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

    Migration0011BottleRouterConfigEnv().up()

    # World-readable drop-in at the canonical path — NOT the main unit path, so the
    # baseline unit (and the v0002 migration that builds it) is left untouched.
    assert written["path"] == BOTTLE_ROUTER_CONFIG_DROPIN_PATH
    assert written["path"].endswith("openhost.service.d/20-bottle-router-config.conf")
    assert written["mode"] == 0o644
    assert written["content"] == BOTTLE_ROUTER_CONFIG_DROPIN
    # The drop-in only adds the new name; it must not set the legacy directive
    # (the baseline unit already does). A comment may mention the legacy name.
    assert "Environment=BOTTLE_ROUTER_CONFIG=" in written["content"]
    assert "Environment=OPENHOST_ROUTER_CONFIG=" not in written["content"]

    # systemd is reloaded so the drop-in is authoritative for the next start.
    assert ("systemctl", "daemon-reload") in run_calls
    # It must NOT restart openhost itself — the apply walk does that at the end;
    # a mid-migration restart would kill the running apply process.
    assert not any(c[:2] == ("systemctl", "restart") for c in run_calls)


def test_migration_version_is_eleven() -> None:
    assert v0011_bottle_router_config_env.Migration0011BottleRouterConfigEnv.version == 11
