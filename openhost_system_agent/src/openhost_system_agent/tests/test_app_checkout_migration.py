import os
import pwd
import sqlite3
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from openhost_system_agent.migrations.versions import v0015_host_only_app_checkouts as migration


@pytest.fixture
def checkout_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "temporary_data"
    source = root / "app_temp_data/notes/repo"
    source.mkdir(parents=True)
    (source / "source.txt").write_text("installed source")
    (source.parent / "scratch.txt").write_text("scratch")
    db_path = tmp_path / "router.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE apps (name TEXT PRIMARY KEY, repo_path TEXT, container_id TEXT, status TEXT, error_message TEXT)"
        )
        db.execute("INSERT INTO apps VALUES (?, ?, ?, ?, NULL)", ("notes", str(source), "notes-container", "running"))
        db.execute(
            "INSERT INTO apps VALUES (?, ?, ?, ?, NULL)", ("peer", "/host/builtin", "peer-container", "running")
        )
    monkeypatch.setattr(migration, "_stop_containers", Mock())
    monkeypatch.setattr(migration, "_require_router_stopped", Mock())
    return db_path, root, source, root / "app_checkouts/notes"


def _stored_path(db_path: Path) -> str:
    with sqlite3.connect(db_path) as db:
        return str(db.execute("SELECT repo_path FROM apps WHERE name = 'notes'").fetchone()[0])


def test_moves_checkout_preserving_scratch_and_status(checkout_layout: tuple[Path, Path, Path, Path]) -> None:
    db_path, root, source, destination = checkout_layout
    migration.migrate(db_path, root)
    assert not source.exists()
    assert (destination / "source.txt").read_text() == "installed source"
    assert (source.parent / "scratch.txt").read_text() == "scratch"
    assert _stored_path(db_path) == str(destination)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT status FROM apps WHERE name = 'notes'").fetchone()[0] == "running"
        assert db.execute("SELECT repo_path FROM apps WHERE name = 'peer'").fetchone()[0] == "/host/builtin"
    cast(Mock, migration._stop_containers).assert_called_once_with(["notes-container", "peer-container"])
    migration.migrate(db_path, root)
    cast(Mock, migration._stop_containers).assert_called_once()


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
    monkeypatch.setattr(migration, "_stop_containers", Mock(side_effect=subprocess.TimeoutExpired("podman", 120)))
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


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "reloading"])
def test_live_router_is_rejected_before_stopping_apps(
    checkout_layout: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    db_path, root, source, destination = checkout_layout
    # Use the real guard, which the layout fixture otherwise stubs.
    monkeypatch.setattr(migration, "_require_router_stopped", _REAL_REQUIRE_ROUTER_STOPPED)
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, state)))
    with pytest.raises(RuntimeError, match="Stop openhost.service"):
        migration.migrate(db_path, root)
    cast(Mock, migration._stop_containers).assert_not_called()
    assert source.is_dir() and not destination.exists()


_REAL_REQUIRE_ROUTER_STOPPED = migration._require_router_stopped


@pytest.mark.parametrize("recorded_ids", [[], ["recorded"]])
def test_stop_includes_managed_containers_missing_from_db(
    monkeypatch: pytest.MonkeyPatch, recorded_ids: list[str]
) -> None:
    monkeypatch.setattr(
        pwd, "getpwnam", Mock(return_value=pwd.struct_passwd(("host", "x", 4242, 4242, "", "/home/host", "/bin/bash")))
    )
    run = Mock(side_effect=[subprocess.CompletedProcess([], 0, "unrecorded\n"), subprocess.CompletedProcess([], 0)])
    monkeypatch.setattr(subprocess, "run", run)
    migration._stop_containers(recorded_ids)
    listing, stopping = run.call_args_list
    assert listing.args[0][-6:] == ["ps", "--all", "--filter", "name=^openhost-", "--format", "{{.ID}}"]
    assert stopping.args[0][-len(recorded_ids) - 1 :] == [*recorded_ids, "unrecorded"]
    assert stopping.kwargs["check"] is True
    assert "XDG_RUNTIME_DIR=/run/user/4242" in stopping.args[0]
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/4242/bus" in stopping.args[0]


def test_router_startup_stops_containers_as_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(side_effect=[subprocess.CompletedProcess([], 0, "unrecorded\n"), subprocess.CompletedProcess([], 0)])
    monkeypatch.setattr(subprocess, "run", run)
    migration._stop_containers([], current_user=True)
    assert run.call_args_list[0].args[0][:2] == ["podman", "ps"]
    assert run.call_args_list[1].args[0] == ["podman", "stop", "--ignore", "--time", "10", "--", "unrecorded"]
