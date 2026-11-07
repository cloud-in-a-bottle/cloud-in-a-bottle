import asyncio
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from compute_space.config import Config
from compute_space.core import default_apps
from compute_space.core.app_checkouts import app_checkout_dir
from compute_space.core.app_id import new_app_id
from compute_space.core.checkout_operations import run_checkout_operation
from compute_space.core.manifest import AppManifest
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.exceptions import ConflictException
from compute_space.web.routes.api import apps as routes


def _seed_app(config: Config, name: str, port: int) -> tuple[str, Path]:
    app_id = new_app_id()
    checkout = Path(app_checkout_dir(config.temporary_data_dir, name))
    for directory in (
        checkout,
        Path(config.persistent_data_dir) / "app_data" / name,
        Path(config.temporary_data_dir) / "app_temp_data" / name,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (checkout / "source.txt").write_text(name)
    with sqlite3.connect(config.db_path) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES (?, ?, '1', ?, ?, 'stopped')",
            (app_id, name, str(checkout), port),
        )
    return app_id, checkout


def test_cancelled_request_retains_operation_lock_and_connection(tmp_path: Path) -> None:
    db_path = str(tmp_path / "db.sqlite")
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE results (value TEXT)")
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def first(db: sqlite3.Connection) -> None:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        started.set()
        assert release.wait(5)
        db.execute("INSERT INTO results VALUES ('first')")
        db.commit()

    def second(db: sqlite3.Connection) -> list[str]:
        second_started.set()
        return [row[0] for row in db.execute("SELECT value FROM results")]

    async def exercise() -> None:
        task = asyncio.create_task(run_checkout_operation(db_path, first))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            follower = asyncio.create_task(run_checkout_operation(db_path, second))
            await asyncio.sleep(0.05)
            assert not second_started.is_set()
        finally:
            release.set()
        assert await follower == ["first"]

    asyncio.run(exercise())


def test_concurrent_renames_do_not_delete_winning_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, port=20500)
    init_db(config.db_path)
    first_id, first_path = _seed_app(config, "first", 19500)
    second_id, second_path = _seed_app(config, "second", 19501)
    monkeypatch.setattr(routes, "stop_app_process", Mock())
    entered = threading.Event()
    release = threading.Event()
    rename = routes._rename_app_storage_dirs

    def pause_first(config: Config, old: str, new: str, archive: str) -> str | None:
        if old == "first":
            entered.set()
            assert release.wait(5)
        return rename(config, old, new, archive)

    monkeypatch.setattr(routes, "_rename_app_storage_dirs", pause_first)

    async def exercise() -> None:
        first = asyncio.create_task(routes.rename_app.fn(first_id, routes.RenameAppRequest("target"), config))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(routes.rename_app.fn(second_id, routes.RenameAppRequest("target"), config))
            await asyncio.sleep(0.05)
        finally:
            release.set()
        assert (await first).status_code == 200
        with pytest.raises(ConflictException):
            await second

    asyncio.run(exercise())
    target = Path(app_checkout_dir(config.temporary_data_dir, "target"))
    assert (target / "source.txt").read_text() == "first"
    assert (second_path / "source.txt").read_text() == "second"
    assert not first_path.exists()


def test_install_reserves_checkout_until_record_is_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, port=20501)
    init_db(config.db_path)
    app_id, original = _seed_app(config, "original", 19500)
    clone = tmp_path / "clone/repo"
    clone.mkdir(parents=True)
    (clone / "cloudinabottle.toml").write_text(
        '[app]\nname="target"\nversion="1"\n[runtime.container]\nimage="Dockerfile"\nport=8080\n'
    )
    (clone / "source.txt").write_text("installed")
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(routes, "stop_app_process", Mock())

    async def pause_pin(*_args: object) -> None:
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)

    def insert(manifest, repo_path, config, db, **kwargs):
        installed_id = new_app_id()
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES (?, 'target', '1', ?, 19501, 'stopped')",
            (installed_id, repo_path),
        )
        db.commit()
        return installed_id

    monkeypatch.setattr(routes, "_pin_refless_to_landed_branch", pause_pin)
    monkeypatch.setattr(routes, "insert_and_deploy", insert)

    async def exercise() -> None:
        request = routes.AddAppRequest(repo_url="https://example.com/app", clone_dir=str(clone))
        install = asyncio.create_task(routes.api_add_app.fn(request, config))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            rename = asyncio.create_task(routes.rename_app.fn(app_id, routes.RenameAppRequest("target"), config))
            await asyncio.sleep(0.05)
        finally:
            release.set()
        assert (await install).status_code == 200
        with pytest.raises(ConflictException):
            await rename

    asyncio.run(exercise())
    target = Path(app_checkout_dir(config.temporary_data_dir, "target"))
    assert (target / "source.txt").read_text() == "installed"
    assert (original / "source.txt").read_text() == "original"


def test_default_install_rolls_back_before_releasing_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, port=20502)
    init_db(config.db_path)
    clone = tmp_path / "clone/repo"
    clone.mkdir(parents=True)

    def fail_insert(manifest, repo_path, config, db, **kwargs):
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES (?, 'target', '1', ?, 19501, 'building')",
            (new_app_id(), repo_path),
        )
        raise RuntimeError("insert failed")

    monkeypatch.setattr(default_apps, "insert_and_deploy", fail_insert)
    with sqlite3.connect(config.db_path) as db:
        status, error = default_apps._finalize_install(
            AppManifest(name="target", version="1"),
            str(clone),
            str(clone.parent),
            config,
            db,
            repo_url="https://example.com/app",
        )
        assert status == "failed" and error is not None
        assert not db.in_transaction
    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT 1 FROM apps WHERE name = 'target'").fetchone() is None
