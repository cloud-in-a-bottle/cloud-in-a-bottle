from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from compute_space.core import apps as apps_mod
from compute_space.core.apps import deploy_app_background
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import run_app_image
from compute_space.core.apps import start_app_process
from compute_space.core.manifest import PortMapping
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config

MANIFEST_TEXT = """\
[app]
name = "launch-test"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[resources]
memory_mb = 384
cpu_cores = 1.5
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    config = _make_test_config(tmp_path, port=20800)
    init_db(config.db_path)
    return config


@pytest.fixture
def app_db(cfg: Any, tmp_path: Path) -> tuple[sqlite3.Connection, str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(MANIFEST_TEXT)
    app_id = "launch-test-id"
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES (?, ?, ?, ?, ?, ?)",
        (app_id, "launch-test", "1.0.0", str(repo), 20801, "building"),
    )
    db.execute(
        "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES (?, ?, ?, ?)",
        (app_id, "metrics", 9090, 22090),
    )
    db.commit()
    try:
        yield db, app_id, repo
    finally:
        db.close()


def test_run_existing_image_prepares_fresh_runtime_from_persisted_app(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)
    runtime_env = {"OPENHOST_APP_TOKEN": "fresh-token", "CUSTOM": "value"}

    with (
        mock.patch.object(apps_mod, "make_data_dirs_and_env_vars", return_value=runtime_env) as prepare,
        mock.patch.object(apps_mod, "run_container", return_value="container-123") as run,
        mock.patch.object(apps_mod, "wait_for_ready", return_value=True),
        mock.patch.object(apps_mod, "build_image") as build,
    ):
        run_app_image(app_id, "registry.example/existing:sha", manifest, db, cfg)

    build.assert_not_called()
    prepare.assert_called_once_with(
        app_id=app_id,
        app_name="launch-test",
        manifest=manifest,
        data_dir=cfg.persistent_data_dir,
        temp_data_dir=cfg.temporary_data_dir,
        archive_dir=cfg.app_archive_dir,
        my_openhost_redirect_domain=cfg.my_openhost_redirect_domain,
        zone_domain="testzone.local",
        port=cfg.port,
        owner_username="owner",
    )
    run.assert_called_once_with(
        "launch-test",
        "registry.example/existing:sha",
        manifest,
        20801,
        runtime_env,
        cfg.persistent_data_dir,
        cfg.temporary_data_dir,
        cfg.app_archive_dir,
        port_mappings=[PortMapping(label="metrics", container_port=9090, host_port=22090)],
    )
    row = db.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert (row["status"], row["container_id"]) == ("running", "container-123")
    token = db.execute("SELECT token_hash FROM app_tokens WHERE app_id = ?", (app_id,)).fetchone()
    assert token["token_hash"] == hashlib.sha256(b"fresh-token").hexdigest()


def test_run_failure_removes_started_container_and_clears_db_reference(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)

    with (
        mock.patch.object(apps_mod, "make_data_dirs_and_env_vars", return_value={}),
        mock.patch.object(apps_mod, "run_container", return_value="container-123"),
        mock.patch.object(apps_mod, "wait_for_ready", side_effect=RuntimeError("readiness crashed")),
        mock.patch.object(apps_mod, "stop_container") as stop,
        pytest.raises(RuntimeError, match="readiness crashed"),
    ):
        run_app_image(app_id, "existing:image", manifest, db, cfg)

    stop.assert_called_once_with("container-123")
    row = db.execute("SELECT status, error_message, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert (row["status"], row["error_message"], row["container_id"]) == ("error", "readiness crashed", None)


def test_runtime_preparation_failure_preserves_existing_container_reference(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)
    db.execute("UPDATE apps SET container_id = ? WHERE app_id = ?", ("existing-container", app_id))
    db.commit()

    with (
        mock.patch.object(apps_mod, "make_data_dirs_and_env_vars", side_effect=RuntimeError("storage unavailable")),
        mock.patch.object(apps_mod, "run_container") as run,
        mock.patch.object(apps_mod, "stop_container") as stop,
        pytest.raises(RuntimeError, match="storage unavailable"),
    ):
        run_app_image(app_id, "existing:image", manifest, db, cfg)

    run.assert_not_called()
    stop.assert_not_called()
    row = db.execute("SELECT status, error_message, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert (row["status"], row["error_message"], row["container_id"]) == (
        "error",
        "storage unavailable",
        "existing-container",
    )


def test_run_failure_preserves_container_reference_when_cleanup_fails(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)

    with (
        mock.patch.object(apps_mod, "make_data_dirs_and_env_vars", return_value={}),
        mock.patch.object(apps_mod, "run_container", return_value="container-123"),
        mock.patch.object(apps_mod, "wait_for_ready", side_effect=RuntimeError("readiness crashed")),
        mock.patch.object(apps_mod, "stop_container", side_effect=RuntimeError("podman unavailable")),
        pytest.raises(RuntimeError, match="readiness crashed"),
    ):
        run_app_image(app_id, "existing:image", manifest, db, cfg)

    row = db.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert (row["status"], row["container_id"]) == ("error", "container-123")


@pytest.mark.parametrize("entry_point", ["start", "deploy"])
def test_existing_start_paths_build_before_running_image(
    entry_point: str,
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, repo = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)
    events: list[tuple[str, str]] = []

    def build(*args: Any, **kwargs: Any) -> str:
        events.append(("build", args[0]))
        return "newly-built:image"

    def run(run_app_id: str, image: str, *args: Any, **kwargs: Any) -> None:
        assert run_app_id == app_id
        events.append(("run", image))

    with (
        mock.patch.object(apps_mod, "build_image", side_effect=build),
        mock.patch.object(apps_mod, "run_app_image", side_effect=run),
    ):
        if entry_point == "start":
            start_app_process(app_id, db, cfg)
        else:
            db.close()
            deploy_app_background(manifest, str(repo), cfg, app_id, "launch-test")

    assert events == [("build", "launch-test"), ("run", "newly-built:image")]


def test_initial_deploy_reuses_prepared_environment(
    cfg: Any,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "initial-repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(MANIFEST_TEXT)
    manifest = parse_manifest_from_string(MANIFEST_TEXT)
    prepared_env = {"OPENHOST_APP_TOKEN": "initial-token"}

    class ImmediateThread:
        def __init__(self, *, target: Any, args: tuple[Any, ...], kwargs: dict[str, Any], daemon: bool) -> None:
            self.target = target
            self.args = args
            self.kwargs = kwargs

        def start(self) -> None:
            self.target(*self.args, **self.kwargs)

    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        with (
            mock.patch.object(apps_mod, "make_data_dirs_and_env_vars", return_value=prepared_env) as prepare,
            mock.patch.object(apps_mod.threading, "Thread", ImmediateThread),
            mock.patch.object(apps_mod, "build_image", return_value="built:image"),
            mock.patch.object(apps_mod, "run_app_image") as run,
        ):
            app_id = insert_and_deploy(manifest, str(repo), cfg, db)
    finally:
        db.close()

    prepare.assert_called_once()
    run.assert_called_once_with(app_id, "built:image", manifest, mock.ANY, cfg, env_vars=prepared_env)


def test_deploy_retries_build_three_times_before_running(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, repo = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)
    db.close()

    with (
        mock.patch.object(
            apps_mod,
            "build_image",
            side_effect=[RuntimeError("transient one"), RuntimeError("transient two"), "built:image"],
        ) as build,
        mock.patch.object(apps_mod.time, "sleep") as sleep,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        deploy_app_background(manifest, str(repo), cfg, app_id, "launch-test")

    assert build.call_count == 3
    assert sleep.call_args_list == [mock.call(5), mock.call(10)]
    run.assert_called_once_with(app_id, "built:image", manifest, mock.ANY, cfg, env_vars=None)


def test_start_build_failure_is_not_retried(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db

    with (
        mock.patch.object(apps_mod, "build_image", side_effect=RuntimeError("build failed")) as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
        pytest.raises(RuntimeError, match="build failed"),
    ):
        start_app_process(app_id, db, cfg)

    build.assert_called_once()
    run.assert_not_called()
