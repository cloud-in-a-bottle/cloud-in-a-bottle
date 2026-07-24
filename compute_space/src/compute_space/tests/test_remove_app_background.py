"""Unit tests for :func:`compute_space.core.apps.remove_app_background`."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from compute_space.core.app_id import new_app_id
from compute_space.core.apps import remove_app_background
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config


def _seed_app_with_children(db_path: str, app_name: str = "myapp") -> str:
    """Insert an app row plus one row in each ON-DELETE-CASCADE child. Returns app_id."""
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, ?, '1.0', '/repo', 19500, 'removing')",
            (app_id, app_name),
        )
        db.execute(
            "INSERT INTO app_databases (app_id, db_name, db_path) VALUES (?, 'main', '/data/main.db')",
            (app_id,),
        )
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES (?, 'http', 8080, 19501)",
            (app_id,),
        )
        db.execute("INSERT INTO app_tokens (app_id, token_hash) VALUES (?, 'fakehash')", (app_id,))
        db.execute(
            "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) "
            "VALUES ('https://e.x/s', ?, '1.0', '/svc')",
            (app_id,),
        )
        db.execute(
            "INSERT INTO permissions_v2 (consumer_app_id, service_url, grant_payload) VALUES (?, 'u', '{}')",
            (app_id,),
        )
        db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES ('https://e.x/s', ?)", (app_id,))
        db.commit()
    finally:
        db.close()
    return app_id


def _table_has_app(db_path: str, table: str, app_id: str, key_col: str = "app_id") -> bool:
    db = sqlite3.connect(db_path)
    try:
        cur = db.execute(f"SELECT 1 FROM {table} WHERE {key_col} = ?", (app_id,))
        return cur.fetchone() is not None
    finally:
        db.close()


def test_remove_cascades_to_all_child_tables(tmp_path: Path) -> None:
    """``DELETE FROM apps`` must cascade to every ON-DELETE-CASCADE child.

    The worker opens its own SQLite connection; FK enforcement is
    per-connection and OFF by default, so the worker has to issue
    ``PRAGMA foreign_keys = ON`` or the cascades silently no-op and
    orphan rows accumulate in app_tokens / permissions / etc.
    """
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data"),
        patch("compute_space.core.apps.deprovision_temp_data"),
    ):
        remove_app_background(app_id, keep_data=False, config=cfg)

    assert not _table_has_app(cfg.db_path, "apps", app_id, key_col="app_id")
    for table, key_col in [
        ("app_databases", "app_id"),
        ("app_port_mappings", "app_id"),
        ("app_tokens", "app_id"),
        ("service_providers_v2", "app_id"),
        ("permissions_v2", "consumer_app_id"),
        ("service_defaults", "app_id"),
    ]:
        assert not _table_has_app(cfg.db_path, table, app_id, key_col=key_col), (
            f"{table}.{key_col} still has a row for app_id={app_id!r} — FK cascade did not fire"
        )


def test_remove_keep_data_calls_temp_only(tmp_path: Path) -> None:
    """``keep_data=True`` must hit the temp-only deprovision path."""
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        remove_app_background(app_id, keep_data=True, config=cfg)

    full.assert_not_called()
    temp_only.assert_called_once_with("myapp", cfg.temporary_data_dir)


def test_remove_full_calls_full_deprovision(tmp_path: Path) -> None:
    """``keep_data=False`` must hit the full deprovision."""
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        remove_app_background(app_id, keep_data=False, config=cfg)

    temp_only.assert_not_called()
    # The archive tier is always the JuiceFS mountpoint, so remove passes
    # the effective archive dir = app_archive_dir for every backend.
    full.assert_called_once_with(
        "myapp",
        cfg.persistent_data_dir,
        cfg.temporary_data_dir,
        cfg.app_archive_dir,
    )


def test_remove_proceeds_when_deprovision_raises(tmp_path: Path) -> None:
    """A deprovision failure must not block the row delete; the row
    would otherwise be stuck in 'removing' forever and the user could
    never re-deploy."""
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data", side_effect=OSError("disk on fire")),
        patch("compute_space.core.apps.deprovision_temp_data"),
    ):
        remove_app_background(app_id, keep_data=False, config=cfg)

    assert not _table_has_app(cfg.db_path, "apps", app_id, key_col="app_id")


def test_remove_returns_quietly_when_app_already_gone(tmp_path: Path) -> None:
    """Calling the worker for a non-existent app must be a clean no-op."""
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)

    with (
        patch("compute_space.core.apps.stop_app_process") as stop,
        patch("compute_space.core.apps.remove_image") as rmi,
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        remove_app_background(new_app_id(), keep_data=False, config=cfg)

    stop.assert_not_called()
    rmi.assert_not_called()
    full.assert_not_called()
    temp_only.assert_not_called()


def test_remove_records_error_when_db_delete_path_explodes(tmp_path: Path) -> None:
    """If the DELETE itself fails, the row should land in 'error' so
    the operator isn't left staring at a permanent 'removing' indicator.
    """
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app_with_children(cfg.db_path, "myapp")

    real_connect = sqlite3.connect

    class _ExplodingConn:
        # Forwards every call to the real connection except DELETE FROM apps,
        # which raises. ``__setattr__`` is forwarded too so that
        # ``db.row_factory = sqlite3.Row`` takes effect on the underlying
        # connection (otherwise reads come back as plain tuples and the
        # test exercises an unintended TypeError path).
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def execute(self, *args, **kwargs):
            sql = args[0] if args else ""
            if sql.startswith("DELETE FROM apps"):
                raise sqlite3.OperationalError("simulated DELETE failure")
            return self._real.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

    def _wrap(*args, **kwargs):
        return _ExplodingConn(real_connect(*args, **kwargs))

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data"),
        patch("compute_space.core.apps.deprovision_temp_data"),
        patch("compute_space.core.apps.sqlite3.connect", side_effect=_wrap),
    ):
        remove_app_background(app_id, keep_data=False, config=cfg)

    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status, error_message FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row is not None
    assert row[0] == "error"
    assert "Removal failed" in (row[1] or "")
