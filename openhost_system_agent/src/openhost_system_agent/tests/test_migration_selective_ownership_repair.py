from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.runner import apply_system_migrations
from openhost_system_agent.migrations.versions import v0014_selective_ownership_repair
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


@pytest.mark.parametrize("fail_first_reload", [False, True])
def test_v13_host_converges_script_and_unit_before_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_first_reload: bool
) -> None:
    unit = tmp_path / "openhost.service"
    unit.write_text("[Service]\nExecStart=/home/host/.pixi/bin/pixi run --as-is python -m compute_space\n")
    unit.chmod(0o600)
    script = tmp_path / "openhost-reclaim-pixi"
    script.write_text("#!/bin/sh\nchown -Rh host:host /home/host/openhost\n")
    script.chmod(0o600)
    ledger = tmp_path / "migrations.jsonl"
    ledger.write_text('{"version":13,"success":true}\n')
    calls: list[tuple[str, ...]] = []

    def reload(*cmd: str) -> None:
        assert unit.read_text() == build_openhost_service_unit(4321)
        assert script.read_text() == RECLAIM_SCRIPT
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
        assert stat.S_IMODE(unit.stat().st_mode) == 0o644
        calls.append(cmd)
        if fail_first_reload and len(calls) == 1:
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(v0014_selective_ownership_repair, "OPENHOST_SERVICE_PATH", str(unit))
    monkeypatch.setattr(v0014_selective_ownership_repair, "RECLAIM_SCRIPT_PATH", str(script))
    monkeypatch.setattr(v0014_selective_ownership_repair, "get_host_uid", lambda: 4321)
    monkeypatch.setattr(v0014_selective_ownership_repair, "run", reload)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    registry = [migration for migration in REGISTRY if migration.version <= 14]

    if fail_first_reload:
        with pytest.raises(subprocess.CalledProcessError):
            apply_system_migrations(str(ledger), registry)
        assert current_host_version(read_log(str(ledger))) == 13
    assert apply_system_migrations(str(ledger), registry) == [14]
    assert current_host_version(read_log(str(ledger))) == 14
    expected = [("systemctl", "daemon-reload")] * (2 if fail_first_reload else 1)
    assert calls == expected
    completed = ledger.read_text()
    assert apply_system_migrations(str(ledger), registry) == []
    assert ledger.read_text() == completed
    assert calls == expected
