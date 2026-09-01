"""Tests that reloading an app re-syncs ALL manifest-derived columns.

Regression coverage for: changing a resource limit (e.g. cpu_cores) in the
manifest and updating the app left the DB columns stale — the running
container picked up the new value but the dashboard/diagnostics kept showing
the install-time value. Reload must now write the same manifest-derived column
set that install writes.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from compute_space.core import apps as apps_mod
from compute_space.core.app_id import new_app_id
from compute_space.core.apps import _manifest_column_values
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import reload_app_background
from compute_space.core.manifest import parse_manifest
from compute_space.db.connection import init_db
from compute_space.db.schema import schema_path

from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("reload-sync"), port=20700)
    init_db(c.db_path)
    return c


_MANIFEST = """\
[app]
name = "reload-app"
version = "{version}"
description = "{description}"

[runtime.container]
image = "Dockerfile"
port = {port}

[routing]
health_check = "{health_check}"
public_paths = [{public_paths}]

[resources]
memory_mb = {memory_mb}
cpu_cores = {cpu_cores}
"""


def _write_manifest(repo: Path, **kw: Any) -> None:
    defaults = {
        "version": "1.0.0",
        "description": "desc",
        "port": 5000,
        "health_check": "/health",
        "public_paths": "",
        "memory_mb": 64,
        "cpu_cores": 0.1,
    }
    defaults.update(kw)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "cloudinabottle.toml").write_text(_MANIFEST.format(**defaults))


def _seed_app(cfg: Any, repo_path: str, *, cpu_cores: float, memory_mb: int) -> str:
    """Seed an installed app row with given (install-time) resource limits."""
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps
               (app_id, name, manifest_name, version, description, runtime_type, repo_path,
                health_check, local_port, container_port, memory_mb, cpu_cores, gpu,
                public_paths, links, manifest_raw, status)
               VALUES (?, 'reload-app', 'reload-app', '0.0.1', 'old', 'serverfull', ?,
                       '/old-health', 20701, 5000, ?, ?, 0, '[]', '[]', 'old-raw', 'stopped')""",
            (app_id, repo_path, memory_mb, cpu_cores),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _row(cfg: Any, app_id: str) -> sqlite3.Row:
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()


# ─── the shared column helper ────────────────────────────────────────────────


def test_manifest_column_values_maps_all_manifest_fields(tmp_path: Path) -> None:
    repo = tmp_path / "m"
    _write_manifest(repo, cpu_cores=2.0, memory_mb=512, version="3.1.4", health_check="/hz")
    manifest = parse_manifest(str(repo))
    cols = _manifest_column_values(manifest)
    assert cols["cpu_cores"] == 2.0
    assert cols["memory_mb"] == 512
    assert cols["version"] == "3.1.4"
    assert cols["health_check"] == "/hz"
    assert cols["container_port"] == 5000
    assert cols["manifest_name"] == "reload-app"
    # runtime_type and gpu are manifest-derived too and must be re-synced on
    # reload (they were part of the stale-column bug); pin their presence so a
    # future refactor can't quietly drop them from the shared write set.
    assert cols["runtime_type"] == "serverfull"
    assert cols["gpu"] == 0
    # Non-manifest columns must NOT be present (they must survive reload).
    for forbidden in ("app_id", "local_port", "repo_path", "repo_url", "status", "container_id"):
        assert forbidden not in cols


# ─── reload re-syncs the DB ───────────────────────────────────────────────────


def test_reload_updates_cpu_and_memory_in_db(cfg: Any, tmp_path: Path) -> None:
    """The core regression: a manifest with a new cpu_cores/memory_mb is written
    back to the DB on reload (not left at the install-time value)."""
    repo = tmp_path / "repo"
    _write_manifest(repo, cpu_cores=1.5, memory_mb=1024)
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)

    # start_app_process does the build/run; stub it out so this stays a fast,
    # hermetic DB test (no podman).
    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    assert row["cpu_cores"] == 1.5
    assert row["memory_mb"] == 1024


def test_reload_does_not_persist_new_access_all_archive(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_manifest(repo)
    manifest_path = repo / "cloudinabottle.toml"
    manifest_path.write_text(manifest_path.read_text() + "\n[data]\naccess_all_archive = true\n")
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)

    with mock.patch.object(apps_mod, "start_app_process") as start:
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    assert row["manifest_raw"] == "old-raw"
    assert row["status"] == "error"
    assert "access_all_archive has been removed" in row["error_message"]
    start.assert_not_called()


def test_reload_syncs_all_manifest_columns(cfg: Any, tmp_path: Path) -> None:
    """Every manifest-derived column is refreshed on reload, not just the old
    subset (public_paths/links/manifest_raw/name)."""
    repo = tmp_path / "repo"
    _write_manifest(
        repo,
        cpu_cores=4.0,
        memory_mb=2048,
        version="9.9.9",
        description="new description",
        health_check="/new-health",
        port=8080,
        public_paths='"/api/"',
    )
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)

    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    assert row["cpu_cores"] == 4.0
    assert row["memory_mb"] == 2048
    assert row["version"] == "9.9.9"
    assert row["description"] == "new description"
    assert row["health_check"] == "/new-health"
    assert row["container_port"] == 8080
    assert row["manifest_name"] == "reload-app"
    assert "/api/" in row["public_paths"]
    assert "cpu_cores = 4.0" in row["manifest_raw"]


def test_reload_resyncs_runtime_type_and_gpu(cfg: Any, tmp_path: Path) -> None:
    """runtime_type and gpu are manifest-derived and must be rewritten on
    reload. Seed a row whose stored values differ from the manifest and assert
    reload restores the manifest-derived values (not the stale ones)."""
    repo = tmp_path / "repo"
    _write_manifest(repo, cpu_cores=2.0)  # manifest: runtime_type=serverfull, gpu=false
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)
    # Make the stored row drift from the manifest.
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute("UPDATE apps SET runtime_type = 'stale-type', gpu = 1 WHERE app_id = ?", (app_id,))
        db.commit()
    finally:
        db.close()

    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    assert row["runtime_type"] == "serverfull"
    assert row["gpu"] == 0


def test_reload_preserves_non_manifest_columns(cfg: Any, tmp_path: Path) -> None:
    """Reload must not clobber install-time identity/location columns."""
    repo = tmp_path / "repo"
    _write_manifest(repo, cpu_cores=2.0)
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)

    before = _row(cfg, app_id)
    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)
    after = _row(cfg, app_id)

    assert after["app_id"] == before["app_id"]
    assert after["name"] == before["name"]
    assert after["repo_path"] == before["repo_path"]
    assert after["local_port"] == before["local_port"]


def test_reload_with_millicores_manifest_updates_db(cfg: Any, tmp_path: Path) -> None:
    """A manifest still using the deprecated cpu_millicores is normalized to
    cpu_cores and written to the DB on reload (the migration scenario in the
    bug report)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(
        "[app]\n"
        'name = "reload-app"\n'
        'version = "1.0.0"\n'
        "[runtime.container]\n"
        'image = "Dockerfile"\n'
        "port = 5000\n"
        "[resources]\n"
        "memory_mb = 128\n"
        "cpu_millicores = 2000\n"  # deprecated -> 2.0 cores
    )
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)

    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    assert row["cpu_cores"] == 2.0
    assert row["memory_mb"] == 128


def test_reload_defaults_when_resources_omitted(cfg: Any, tmp_path: Path) -> None:
    """A manifest with no [resources] section resolves to the manifest defaults
    (memory_mb=128, cpu_cores=0.1); reload must write those defaults over
    whatever stale values the row held, not leave the old values in place."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(
        '[app]\nname = "reload-app"\nversion = "1.0.0"\n[runtime.container]\nimage = "Dockerfile"\nport = 5000\n'
    )
    # Seed with non-default limits so a no-op reload would be detectable.
    app_id = _seed_app(cfg, str(repo), cpu_cores=9.9, memory_mb=999)

    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    assert row["memory_mb"] == 128
    assert row["cpu_cores"] == 0.1


def test_reload_syncs_public_paths_and_links(cfg: Any, tmp_path: Path) -> None:
    """public_paths and links are serialized to JSON and re-synced on reload."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(
        "[app]\n"
        'name = "reload-app"\n'
        'version = "1.0.0"\n'
        "[runtime.container]\n"
        'image = "Dockerfile"\n'
        "port = 5000\n"
        "[routing]\n"
        'public_paths = ["/pub/", "/assets/"]\n'
        "[[links]]\n"
        'name = "Docs"\n'
        'path = "/docs"\n'
    )
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)

    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    row = _row(cfg, app_id)
    public_paths = json.loads(row["public_paths"])
    links = json.loads(row["links"])
    assert public_paths == ["/pub/", "/assets/"]
    assert links == [{"name": "Docs", "path": "/docs"}]


def test_reload_with_unparseable_manifest_leaves_columns_unchanged(cfg: Any, tmp_path: Path) -> None:
    """If the manifest can't be parsed on reload, the ValueError branch is hit
    and NONE of the manifest-derived columns are touched — the row keeps its
    prior values rather than being wiped or half-written."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text("this is = not [ valid toml")
    app_id = _seed_app(cfg, str(repo), cpu_cores=0.1, memory_mb=64)
    before = _row(cfg, app_id)

    with mock.patch.object(apps_mod, "start_app_process"):
        reload_app_background(app_id, str(repo), cfg)

    after = _row(cfg, app_id)
    for col in (
        "version",
        "description",
        "manifest_name",
        "health_check",
        "container_port",
        "memory_mb",
        "cpu_cores",
        "gpu",
        "public_paths",
        "links",
        "manifest_raw",
        "runtime_type",
    ):
        assert after[col] == before[col], f"{col} changed on failed reload"


# ─── install / reload share the same manifest column set ─────────────────────


def test_manifest_helper_keys_are_all_real_apps_columns() -> None:
    """Every key the helper emits must be an actual column on the apps table.

    The install/reload SQL is built dynamically from these keys, so a typo or a
    column removed from the schema would surface only as a runtime SQL error on
    a real install/reload. Pin the invariant here instead."""
    conn = sqlite3.connect(":memory:")
    try:
        with open(schema_path()) as f:
            conn.executescript(f.read())
        apps_columns = {r[1] for r in conn.execute("PRAGMA table_info(apps)")}
    finally:
        conn.close()

    # Any manifest works here; we only inspect the helper's keys.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_manifest(p, cpu_cores=1.0, memory_mb=128)
        manifest = parse_manifest(str(p))
    cols = _manifest_column_values(manifest)
    missing = set(cols) - apps_columns
    assert not missing, f"helper emits non-existent apps columns: {missing}"


def test_install_writes_same_manifest_columns_as_helper(cfg: Any, tmp_path: Path) -> None:
    """insert_and_deploy must persist exactly the manifest-derived values the
    shared helper produces, so install and reload can't drift."""
    repo = tmp_path / "repo"
    _write_manifest(
        repo,
        cpu_cores=1.5,
        memory_mb=256,
        version="7.7.7",
        description="installed app",
        health_check="/ok",
        port=6060,
        public_paths='"/p/"',
    )
    manifest = parse_manifest(str(repo))
    expected = _manifest_column_values(manifest)

    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        # Stub the background build thread so no container work happens.
        with mock.patch.object(apps_mod, "deploy_app_background"):
            app_id = insert_and_deploy(manifest, str(repo), cfg, db)
        row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()

    for col, value in expected.items():
        assert row[col] == value, f"install wrote {col}={row[col]!r}, expected {value!r}"
    # Non-manifest install-only columns are set by insert_and_deploy itself.
    assert row["status"] == "building"
    assert row["repo_path"] == str(repo)
