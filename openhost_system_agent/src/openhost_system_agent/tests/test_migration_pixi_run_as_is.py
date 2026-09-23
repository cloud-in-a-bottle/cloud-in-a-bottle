from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.runner import apply_system_migrations
from openhost_system_agent.migrations.versions import v0013_pixi_run_as_is
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


@pytest.mark.parametrize("fail_first_reload", [False, True], ids=["first-try", "retry-after-reload-failure"])
def test_existing_host_rewrites_unit_before_reload_and_skips_reapplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_first_reload: bool
) -> None:
    unit_path = tmp_path / "openhost.service"
    unit_path.write_text("[Service]\nExecStart=/home/host/.pixi/bin/pixi run python -m compute_space\n")
    unit_path.chmod(0o644)
    log_path = tmp_path / "migrations.jsonl"
    log_path.write_text('{"version":12,"success":true}\n')
    expected_unit = build_openhost_service_unit(1234)
    calls: list[tuple[str, ...]] = []

    def run(*cmd: str) -> None:
        # Reload must observe the new unit, and must not start it before the
        # updater has installed dependencies for the destination checkout.
        assert unit_path.read_text() == expected_unit
        calls.append(cmd)
        if fail_first_reload and len(calls) == 1:
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(v0013_pixi_run_as_is, "OPENHOST_SERVICE_PATH", str(unit_path))
    monkeypatch.setattr(v0013_pixi_run_as_is, "get_host_uid", lambda: 1234)
    monkeypatch.setattr(v0013_pixi_run_as_is, "run", run)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    registry = [migration for migration in REGISTRY if migration.version <= 13]

    if fail_first_reload:
        with pytest.raises(subprocess.CalledProcessError, match="daemon-reload"):
            apply_system_migrations(str(log_path), registry)
        failed_log = read_log(str(log_path))
        assert current_host_version(failed_log) == 12
        assert failed_log[-1].version == 13
        assert failed_log[-1].success is False
        assert "daemon-reload" in (failed_log[-1].error or "")

    assert apply_system_migrations(str(log_path), registry) == [13]
    assert unit_path.read_text() == expected_unit
    assert stat.S_IMODE(unit_path.stat().st_mode) == 0o644
    expected_calls = [("systemctl", "daemon-reload")] * (2 if fail_first_reload else 1)
    assert calls == expected_calls
    assert current_host_version(read_log(str(log_path))) == 13
    assert sum(entry.version == 13 and entry.success for entry in read_log(str(log_path))) == 1
    completed_log = log_path.read_text()

    assert apply_system_migrations(str(log_path), registry) == []
    assert log_path.read_text() == completed_log
    assert calls == expected_calls
