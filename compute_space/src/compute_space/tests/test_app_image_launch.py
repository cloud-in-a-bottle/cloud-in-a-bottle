from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from compute_space.core import apps as apps_mod
from compute_space.core.apps import deploy_app_background
from compute_space.core.apps import launch_app_image
from compute_space.core.apps import restart_app_process
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


def test_launch_existing_image_prepares_fresh_runtime_from_persisted_app(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)
    runtime_env = {"OPENHOST_APP_TOKEN": "fresh-token", "CUSTOM": "value"}

    with (
        mock.patch.object(apps_mod, "provision_data", return_value=runtime_env) as provision,
        mock.patch.object(apps_mod, "run_container", return_value="container-123") as run,
        mock.patch.object(apps_mod, "wait_for_ready", return_value=True),
        mock.patch.object(apps_mod, "build_image") as build,
    ):
        launch_app_image(app_id, "registry.example/existing:sha", manifest, db, cfg)

    build.assert_not_called()
    provision.assert_called_once_with(
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


def test_launch_failure_removes_started_container_and_clears_db_reference(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)

    with (
        mock.patch.object(apps_mod, "provision_data", return_value={}),
        mock.patch.object(apps_mod, "run_container", return_value="container-123"),
        mock.patch.object(apps_mod, "wait_for_ready", side_effect=RuntimeError("readiness crashed")),
        mock.patch.object(apps_mod, "stop_container") as stop,
        pytest.raises(RuntimeError, match="readiness crashed"),
    ):
        launch_app_image(app_id, "existing:image", manifest, db, cfg)

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
        mock.patch.object(apps_mod, "provision_data", side_effect=RuntimeError("storage unavailable")),
        mock.patch.object(apps_mod, "run_container") as run,
        mock.patch.object(apps_mod, "stop_container") as stop,
        pytest.raises(RuntimeError, match="storage unavailable"),
    ):
        launch_app_image(app_id, "existing:image", manifest, db, cfg)

    run.assert_not_called()
    stop.assert_not_called()
    row = db.execute("SELECT status, error_message, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert (row["status"], row["error_message"], row["container_id"]) == (
        "error",
        "storage unavailable",
        "existing-container",
    )


def test_launch_failure_preserves_container_reference_when_cleanup_fails(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    manifest = parse_manifest_from_string(MANIFEST_TEXT)

    with (
        mock.patch.object(apps_mod, "provision_data", return_value={}),
        mock.patch.object(apps_mod, "run_container", return_value="container-123"),
        mock.patch.object(apps_mod, "wait_for_ready", side_effect=RuntimeError("readiness crashed")),
        mock.patch.object(apps_mod, "stop_container", side_effect=RuntimeError("podman unavailable")),
        pytest.raises(RuntimeError, match="readiness crashed"),
    ):
        launch_app_image(app_id, "existing:image", manifest, db, cfg)

    row = db.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert (row["status"], row["container_id"]) == ("error", "container-123")


def test_restart_reuses_tagged_image_and_persisted_manifest_without_build(
    cfg: Any,
    app_db: tuple[sqlite3.Connection, str, Path],
) -> None:
    db, app_id, _ = app_db
    db.execute(
        "UPDATE apps SET manifest_raw = ?, container_id = ? WHERE app_id = ?",
        (MANIFEST_TEXT, "old-container", app_id),
    )
    db.commit()

    with (
        mock.patch.object(apps_mod, "launch_app_image") as launch,
        mock.patch.object(apps_mod, "build_image") as build,
    ):
        restart_app_process(app_id, db, cfg)

    build.assert_not_called()
    launch.assert_called_once()
    assert launch.call_args.args[:2] == (app_id, "openhost-launch-test:latest")
    assert launch.call_args.args[2].memory_mb == 384


@pytest.mark.parametrize("entry_point", ["start", "deploy"])
def test_existing_start_paths_build_before_launching_image(
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

    def launch(launch_app_id: str, image: str, *args: Any) -> None:
        assert launch_app_id == app_id
        events.append(("launch", image))

    with (
        mock.patch.object(apps_mod, "build_image", side_effect=build),
        mock.patch.object(apps_mod, "launch_app_image", side_effect=launch),
    ):
        if entry_point == "start":
            start_app_process(app_id, db, cfg)
        else:
            db.close()
            deploy_app_background(manifest, str(repo), cfg, app_id, "launch-test")

    assert events == [("build", "launch-test"), ("launch", "newly-built:image")]
