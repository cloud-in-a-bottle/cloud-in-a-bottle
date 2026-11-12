from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from openhost_system_agent.migrations import runner
from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.migration_log import MigrationLogEntry
from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.registry import latest_registry_version
from openhost_system_agent.migrations.registry import validate_registry
from openhost_system_agent.migrations.runner import apply_system_migrations


class MigrationV2(SystemMigration):
    version = 2

    def up(self) -> None:
        pass


class MigrationV3(SystemMigration):
    version = 3

    def up(self) -> None:
        pass


class FailingMigration(SystemMigration):
    version = 2

    def up(self) -> None:
        raise RuntimeError("intentional failure")


def _write_jsonl(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


class TestValidateRegistry:
    def test_empty_registry(self) -> None:
        validate_registry([])

    def test_valid_registry(self) -> None:
        validate_registry([MigrationV2(), MigrationV3()])

    def test_gap_in_registry(self) -> None:
        class V4(SystemMigration):
            version = 4

            def up(self) -> None:
                pass

        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([MigrationV2(), V4()])

    def test_wrong_start(self) -> None:
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([MigrationV3()])


class TestHighestRegisteredVersion:
    def test_empty_registry(self) -> None:
        assert latest_registry_version([]) == 1

    def test_with_migrations(self) -> None:
        assert latest_registry_version([MigrationV2(), MigrationV3()]) == 3


class TestReadLog:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert read_log(str(tmp_path / "nope.jsonl")) == []

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "m.jsonl"
        f.write_text("")
        assert read_log(str(f)) == []

    def test_valid_entries(self, tmp_path: Path) -> None:
        f = tmp_path / "m.jsonl"
        _write_jsonl(f, [{"version": 1, "success": True}])
        entries = read_log(str(f))
        assert len(entries) == 1
        assert entries[0].version == 1
        assert entries[0].success is True

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "m.jsonl"
        f.write_text('not json\n{"version": 1, "success": true}\n\n')
        entries = read_log(str(f))
        assert len(entries) == 1


class TestCurrentVersion:
    def test_empty_entries(self) -> None:
        assert current_host_version([]) == 0

    def test_last_successful(self) -> None:
        entries = [
            MigrationLogEntry(version=1, timestamp="", success=True, error=None),
            MigrationLogEntry(version=2, timestamp="", success=True, error=None),
        ]
        assert current_host_version(entries) == 2

    def test_ignores_failures(self) -> None:
        entries = [
            MigrationLogEntry(version=1, timestamp="", success=True, error=None),
            MigrationLogEntry(version=2, timestamp="", success=False, error="boom"),
        ]
        assert current_host_version(entries) == 1

    def test_retried_success(self) -> None:
        entries = [
            MigrationLogEntry(version=1, timestamp="", success=True, error=None),
            MigrationLogEntry(version=2, timestamp="", success=False, error="first try"),
            MigrationLogEntry(version=2, timestamp="", success=True, error=None),
        ]
        assert current_host_version(entries) == 2


class TestApplySystemMigrations:
    @pytest.fixture(autouse=True)
    def stopped_router(self, monkeypatch: pytest.MonkeyPatch) -> Mock:
        guard = Mock()
        monkeypatch.setattr(runner, "require_router_stopped", guard)
        return guard

    def test_refuses_without_root(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("test must not run as root")
        path = str(tmp_path / "migrations.jsonl")
        with pytest.raises(RuntimeError, match="root"):
            apply_system_migrations(migrations_path=path, registry=[])

    def test_refuses_version_0(self, tmp_path: Path) -> None:
        path = str(tmp_path / "migrations.jsonl")
        with patch("os.geteuid", return_value=0):
            with pytest.raises(RuntimeError, match="No migration history"):
                apply_system_migrations(migrations_path=path, registry=[])

    def test_refuses_downgrade(self, tmp_path: Path) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(path, [{"version": 99, "success": True}])
        with patch("os.geteuid", return_value=0):
            with pytest.raises(RuntimeError, match="only knows up to"):
                apply_system_migrations(migrations_path=str(path), registry=[])

    def test_noop_when_current(self, tmp_path: Path, stopped_router: Mock) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(path, [{"version": 1, "success": True}])
        with patch("os.geteuid", return_value=0):
            applied = apply_system_migrations(migrations_path=str(path), registry=[])
        assert applied == []
        stopped_router.assert_not_called()

    def test_rejects_live_router_without_applying_or_recording_migration(
        self, tmp_path: Path, stopped_router: Mock
    ) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(path, [{"version": 1, "success": True}])
        original = path.read_text()
        stopped_router.side_effect = RuntimeError("Stop openhost.service")
        with patch("os.geteuid", return_value=0), patch.object(MigrationV2, "up") as up:
            with pytest.raises(RuntimeError, match="Stop openhost.service"):
                apply_system_migrations(str(path), [MigrationV2()])
        up.assert_not_called()
        assert path.read_text() == original

    def test_rechecks_before_each_pending_migration(self, tmp_path: Path, stopped_router: Mock) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(path, [{"version": 1, "success": True}])
        stopped_router.side_effect = [None, RuntimeError("Stop openhost.service")]
        with patch("os.geteuid", return_value=0), patch.object(MigrationV3, "up") as up:
            with pytest.raises(RuntimeError, match="Stop openhost.service"):
                apply_system_migrations(str(path), [MigrationV2(), MigrationV3()])
            up.assert_not_called()
            assert current_host_version(read_log(str(path))) == 2
            stopped_router.side_effect = None
            assert apply_system_migrations(str(path), [MigrationV2(), MigrationV3()]) == [3]
            up.assert_called_once()

    def test_applies_pending(self, tmp_path: Path) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(path, [{"version": 1, "success": True}])
        registry = [MigrationV2(), MigrationV3()]
        with patch("os.geteuid", return_value=0):
            applied = apply_system_migrations(migrations_path=str(path), registry=registry)
        assert applied == [2, 3]

        entries = read_log(str(path))
        assert len(entries) == 3
        assert entries[1].version == 2
        assert entries[1].success is True
        assert entries[2].version == 3
        assert entries[2].success is True

    def test_skips_already_applied(self, tmp_path: Path) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(
            path,
            [
                {"version": 1, "success": True},
                {"version": 2, "success": True},
            ],
        )
        registry = [MigrationV2(), MigrationV3()]
        with patch("os.geteuid", return_value=0):
            applied = apply_system_migrations(migrations_path=str(path), registry=registry)
        assert applied == [3]

    def test_logs_failure_and_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(path, [{"version": 1, "success": True}])
        registry = [FailingMigration()]
        with patch("os.geteuid", return_value=0):
            with pytest.raises(RuntimeError, match="intentional failure"):
                apply_system_migrations(migrations_path=str(path), registry=registry)

        entries = read_log(str(path))
        assert len(entries) == 2
        assert entries[1].version == 2
        assert entries[1].success is False
        assert "intentional failure" in (entries[1].error or "")

    def test_replay_after_failure(self, tmp_path: Path) -> None:
        path = tmp_path / "migrations.jsonl"
        _write_jsonl(
            path,
            [
                {"version": 1, "success": True},
                {"version": 2, "success": False, "error": "first try failed"},
            ],
        )
        registry = [MigrationV2()]
        with patch("os.geteuid", return_value=0):
            applied = apply_system_migrations(migrations_path=str(path), registry=registry)
        assert applied == [2]

        entries = read_log(str(path))
        assert entries[-1].version == 2
        assert entries[-1].success is True
