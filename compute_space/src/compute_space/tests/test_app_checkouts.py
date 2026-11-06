import os
import sqlite3
import subprocess
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.app_checkouts import app_checkout_dir
from compute_space.core.apps import move_clone_to_app_checkout_dir
from compute_space.core.containers import build_image
from compute_space.core.containers import run_container
from compute_space.core.containers import stop_container
from compute_space.core.data import deprovision_data
from compute_space.core.data import deprovision_temp_data
from compute_space.core.manifest import AppManifest
from compute_space.tests.test_containers import _run_and_capture
from openhost_system_agent.migrations.versions import v0015_host_only_app_checkouts as migration


@pytest.mark.parametrize("all_data", [False, True])
def test_checkout_is_outside_every_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_data: bool) -> None:
    config = DefaultConfig(data_root_dir=str(tmp_path))
    clone = tmp_path / "clone/repo"
    clone.mkdir(parents=True)
    (clone / "source.txt").write_text("source")
    checkout = Path(move_clone_to_app_checkout_dir(str(clone), "myapp", config))
    assert checkout == Path(config.temporary_data_dir) / "app_checkouts/myapp"
    assert (checkout / "source.txt").read_text() == "source"
    assert not clone.parent.exists()
    manifest = AppManifest(name="myapp", version="1", app_temp_data=True, access_all_app_data=all_data)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    # _run_and_capture uses temp/ as its configured temporary root.
    candidate = Path(app_checkout_dir(str(tmp_path / "temp"), "myapp"))
    mounts = [Path(value.split(":")[0]) for previous, value in zip(argv, argv[1:], strict=False) if previous == "-v"]
    assert mounts
    for mount in mounts:
        assert not candidate.is_relative_to(mount)
    assert all(
        not value.endswith(":ro,idmap") for previous, value in zip(argv, argv[1:], strict=False) if previous == "-v"
    )


@pytest.mark.parametrize("delete_data", [False, True])
def test_removal_cleans_checkout_and_scratch(tmp_path: Path, delete_data: bool) -> None:
    config = DefaultConfig(data_root_dir=str(tmp_path))
    checkout = Path(app_checkout_dir(config.temporary_data_dir, "notes"))
    scratch = Path(config.temporary_data_dir) / "app_temp_data/notes"
    data = Path(config.persistent_data_dir) / "app_data/notes"
    peer_checkout = Path(app_checkout_dir(config.temporary_data_dir, "peer"))
    for directory in (checkout, scratch, data, peer_checkout):
        directory.mkdir(parents=True)
        (directory / "keep.txt").write_text("data")
    if delete_data:
        deprovision_data("notes", config.persistent_data_dir, config.temporary_data_dir, config.app_archive_dir)
    else:
        deprovision_temp_data("notes", config.temporary_data_dir)
    assert not checkout.exists() and not scratch.exists()
    assert data.exists() is not delete_data
    assert (peer_checkout / "keep.txt").read_text() == "data"


@pytest.mark.requires_containers
@pytest.mark.parametrize("all_data", [False, True])
def test_running_app_loses_checkout_access_after_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_data: bool
) -> None:
    name = f"checkout-test-{uuid.uuid4().hex[:12]}"
    config = DefaultConfig(data_root_dir=str(tmp_path))
    config.make_all_dirs()
    temp_root = Path(config.temporary_data_dir)
    source = temp_root / "app_temp_data" / name / "repo"
    source.mkdir(parents=True)
    (source / "Dockerfile").write_text('FROM docker.io/library/alpine:3.21\nCMD ["sleep", "infinity"]\n')
    (source / "source.txt").write_text("source")
    manifest = AppManifest(name=name, version="1", app_temp_data=True, access_all_app_data=all_data)
    for parent in (Path(config.persistent_data_dir) / "app_data", Path(config.app_archive_dir)):
        (parent / name).mkdir(parents=True, exist_ok=True)
    image = build_image(name, str(source), "Dockerfile")
    container_name = f"openhost-{name}"

    def start() -> str:
        return run_container(
            name,
            image,
            manifest,
            local_port=0,
            env_vars={},
            data_dir=config.persistent_data_dir,
            temp_data_dir=config.temporary_data_dir,
            archive_dir=config.app_archive_dir,
        )

    try:
        container_id = start()
        container_repo = f"/data/app_temp_data/{name}/repo"
        subprocess.run(["podman", "exec", container_id, "test", "-f", f"{container_repo}/source.txt"], check=True)
        with sqlite3.connect(config.db_path) as db:
            db.execute("CREATE TABLE apps (name TEXT, repo_path TEXT, container_id TEXT, status TEXT)")
            db.execute("INSERT INTO apps VALUES (?, ?, ?, 'running')", (name, str(source), container_id))

        # CI runs rootless Podman as its own user rather than the production host user.
        stop = Mock(side_effect=lambda ids: subprocess.run(["podman", "stop", "--ignore", *ids], check=True))
        monkeypatch.setattr(migration, "_stop_containers", stop)
        monkeypatch.setattr(migration, "_require_router_stopped", lambda: None)
        migration.migrate(Path(config.db_path), temp_root)
        stop.assert_called_once_with([container_id])
        checkout = Path(app_checkout_dir(config.temporary_data_dir, name))
        assert (checkout / "source.txt").read_text() == "source"
        container_id = start()
        for hidden_path in (container_repo, str(checkout), "/data/app_checkouts"):
            subprocess.run(["podman", "exec", container_id, "test", "!", "-e", hidden_path], check=True)
        subprocess.run(
            ["podman", "exec", container_id, "sh", "-c", f"echo scratch > /data/app_temp_data/{name}/scratch.txt"],
            check=True,
        )
        assert (source.parent / "scratch.txt").read_text() == "scratch\n"
        assert os.stat(source.parent / "scratch.txt").st_uid == os.getuid()
    finally:
        stop_container(container_name)
        subprocess.run(["podman", "rmi", "-f", image], check=True, capture_output=True)
