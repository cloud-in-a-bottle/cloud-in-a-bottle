import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.runner import apply_system_migrations
from openhost_system_agent.migrations.versions import v0015_host_only_app_checkouts as migration


@pytest.fixture
def stopped_containers(monkeypatch: pytest.MonkeyPatch) -> Mock:
    stop = Mock()
    monkeypatch.setattr(migration, "stop_app_containers", stop)
    return stop


@pytest.fixture
def checkout_layout(tmp_path: Path, stopped_containers: Mock) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "temporary_data"
    source = root / "app_temp_data/notes/repo"
    source.mkdir(parents=True)
    (source / "source.txt").write_text("installed source")
    (source.parent / "scratch.txt").write_text("scratch")
    db_path = tmp_path / "persistent_data/openhost/router.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE apps (name TEXT PRIMARY KEY, repo_path TEXT, container_id TEXT, status TEXT, error_message TEXT)"
        )
        db.execute("INSERT INTO apps VALUES (?, ?, ?, ?, NULL)", ("notes", str(source), "notes-container", "running"))
        db.execute(
            "INSERT INTO apps VALUES (?, ?, ?, ?, NULL)", ("peer", "/host/builtin", "peer-container", "running")
        )
    return db_path, root, source, root / "app_checkouts/notes"


def _stored_path(db_path: Path) -> str:
    with sqlite3.connect(db_path) as db:
        return str(db.execute("SELECT repo_path FROM apps WHERE name = 'notes'").fetchone()[0])


def test_moves_checkout_preserving_scratch_and_status(
    checkout_layout: tuple[Path, Path, Path, Path], stopped_containers: Mock
) -> None:
    db_path, root, source, destination = checkout_layout
    migration.migrate(db_path, root)
    assert not source.exists()
    assert (destination / "source.txt").read_text() == "installed source"
    assert (source.parent / "scratch.txt").read_text() == "scratch"
    assert _stored_path(db_path) == str(destination)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT status FROM apps WHERE name = 'notes'").fetchone()[0] == "running"
        assert db.execute("SELECT repo_path FROM apps WHERE name = 'peer'").fetchone()[0] == "/host/builtin"
    stopped_containers.assert_called_once_with(["notes-container", "peer-container"])
    migration.migrate(db_path, root)
    stopped_containers.assert_called_once()


def test_retry_after_rename_before_db_commit(
    checkout_layout: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, root, source, destination = checkout_layout
    with monkeypatch.context() as interrupted:
        interrupted.setattr(os, "fsync", Mock(side_effect=OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            migration.migrate(db_path, root)
    assert not source.exists() and destination.is_dir()
    assert _stored_path(db_path) == str(source)
    migration.migrate(db_path, root)
    assert _stored_path(db_path) == str(destination)
    assert (destination / "source.txt").read_text() == "installed source"


def test_missing_checkout_still_gets_current_path(checkout_layout: tuple[Path, Path, Path, Path]) -> None:
    db_path, root, source, destination = checkout_layout
    (source / "source.txt").unlink()
    source.rmdir()
    migration.migrate(db_path, root)
    assert _stored_path(db_path) == str(destination)
    assert not destination.exists()


def test_retry_after_scratch_directory_removed(checkout_layout: tuple[Path, Path, Path, Path]) -> None:
    db_path, root, source, destination = checkout_layout
    destination.parent.mkdir()
    source.rename(destination)
    (source.parent / "scratch.txt").unlink()
    source.parent.rmdir()
    migration.migrate(db_path, root)
    assert _stored_path(db_path) == str(destination)
    assert (destination / "source.txt").read_text() == "installed source"


def test_collision_preserves_both_trees(checkout_layout: tuple[Path, Path, Path, Path]) -> None:
    db_path, root, source, destination = checkout_layout
    destination.mkdir(parents=True)
    (destination / "other.txt").write_text("other")
    with pytest.raises(RuntimeError, match="Cannot move"):
        migration.migrate(db_path, root)
    assert (source / "source.txt").read_text() == "installed source"
    assert (destination / "other.txt").read_text() == "other"
    assert _stored_path(db_path) == str(source)


@pytest.mark.parametrize("link_location", ["source", "app", "app_temp_data", "destination", "app_checkouts"])
def test_rejects_symlink_paths(
    checkout_layout: tuple[Path, Path, Path, Path], tmp_path: Path, link_location: str
) -> None:
    db_path, root, source, destination = checkout_layout
    match link_location:
        case "source":
            link = source
        case "app":
            link = source.parent
        case "app_temp_data":
            link = source.parent.parent
        case "destination":
            link = destination
        case _:
            link = destination.parent
    target = tmp_path / "outside"
    if link.exists():
        link.rename(target)
    else:
        target.mkdir()
        link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        migration.migrate(db_path, root)
    assert _stored_path(db_path) == str(source)
    assert target.is_dir()


def test_stop_failure_does_not_move_checkout(
    checkout_layout: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, root, source, destination = checkout_layout
    monkeypatch.setattr(migration, "stop_app_containers", Mock(side_effect=subprocess.TimeoutExpired("podman", 120)))
    with pytest.raises(subprocess.TimeoutExpired):
        migration.migrate(db_path, root)
    assert source.is_dir() and not destination.exists()
    assert _stored_path(db_path) == str(source)


def test_migration_uses_configured_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "custom/persistent_data/openhost"
    monkeypatch.setenv("OPENHOST_DATA_DIR", str(data_dir))
    migrate = Mock()
    monkeypatch.setattr(migration, "migrate", migrate)
    migration.Migration0015HostOnlyAppCheckouts().up()
    migrate.assert_called_once_with(data_dir / "router.db", tmp_path / "custom/temporary_data")


def test_missing_database_is_noop(tmp_path: Path) -> None:
    migration.migrate(tmp_path / "router.db", tmp_path / "temporary_data")
    assert not (tmp_path / "router.db").exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_runner_records_checkout_migration_and_retries(
    checkout_layout: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
    stopped_containers: Mock,
) -> None:
    db_path, _root, source, destination = checkout_layout
    ledger = tmp_path / "migrations.jsonl"
    ledger.write_text('{"version":14,"success":true}\n')
    monkeypatch.setenv("OPENHOST_DATA_DIR", str(db_path.parent))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "inactive\n")))
    if interrupted:
        with monkeypatch.context() as failure:
            failure.setattr(os, "fsync", Mock(side_effect=OSError("interrupted")))
            with pytest.raises(OSError, match="interrupted"):
                apply_system_migrations(str(ledger))
        assert current_host_version(read_log(str(ledger))) == 14
        assert _stored_path(db_path) == str(source)
    assert apply_system_migrations(str(ledger)) == [15]
    assert current_host_version(read_log(str(ledger))) == 15
    assert _stored_path(db_path) == str(destination)
    assert (destination / "source.txt").read_text() == "installed source"
    assert not source.exists()
    completed = ledger.read_text()
    stopped_containers.reset_mock()
    assert apply_system_migrations(str(ledger)) == []
    stopped_containers.assert_not_called()
    assert ledger.read_text() == completed
