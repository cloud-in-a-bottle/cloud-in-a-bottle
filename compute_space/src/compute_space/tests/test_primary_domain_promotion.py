from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from compute_space.core import startup
from compute_space.core.domains import AppsBusyForPrimaryChangeError
from compute_space.core.domains import ArchiveMigrationInProgressError
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import PrimaryDomainChangedError
from compute_space.core.domains import primary_domain
from compute_space.core.domains import set_primary_domain
from compute_space.core.domains import upsert_record
from compute_space.core.operation_locks import archive_configuration
from compute_space.core.settings_store import LEGACY_CERT_DOMAIN_KEY
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web import start as start_mod


def _cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com", tls_enabled=True)
    with closing(open_db(cfg)) as db:
        upsert_record(
            db,
            DomainRecord("new.example.com", tls=True, mdns=False, cert_status=DomainCertStatus.ACTIVE),
        )
    return cfg


def _app(db: Any, name: str, status: str, *, container_id: str | None = None) -> None:
    port = 19000 + int(db.execute("SELECT COUNT(*) FROM apps").fetchone()[0])
    db.execute(
        "INSERT INTO apps "
        "(app_id, name, version, repo_path, local_port, status, container_id, manifest_raw, created_at) "
        "VALUES (?, ?, '1', '/tmp/repo', ?, ?, ?, 'manifest', '2000-01-01 00:00:00')",
        (f"{name}-id", name, port, status, container_id),
    )


def test_promotion_atomically_marks_only_active_apps_starting(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "running", "running", container_id="running-container")
        _app(db, "stopped", "stopped")
        _app(db, "live-error", "error", container_id="error-container")
        _app(db, "dead-error", "error")
        db.commit()

        assert set_primary_domain(db, "new.example.com", "old.example.com") is True
        assert primary_domain(db).name == "new.example.com"
        states = {row["name"]: row["status"] for row in db.execute("SELECT name, status FROM apps")}
        assert states == {
            "running": "starting",
            "stopped": "stopped",
            "live-error": "starting",
            "dead-error": "error",
        }
        assert db.execute("SELECT value FROM settings WHERE key = ?", (LEGACY_CERT_DOMAIN_KEY,)).fetchone()[0] == (
            "old.example.com"
        )


def test_stale_promotion_rolls_back_without_touching_apps(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "running", "running", container_id="container")
        db.commit()
        with pytest.raises(PrimaryDomainChangedError):
            set_primary_domain(db, "new.example.com", "stale.example.com")
        assert primary_domain(db).name == "old.example.com"
        assert db.execute("SELECT status FROM apps").fetchone()[0] == "running"
        assert db.execute("SELECT 1 FROM settings WHERE key = ?", (LEGACY_CERT_DOMAIN_KEY,)).fetchone() is None


@pytest.mark.parametrize("status", ["building", "starting", "removing"])
def test_promotion_rejects_active_app_operations(tmp_path: Path, status: str) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "busy", status)
        db.commit()
        with pytest.raises(AppsBusyForPrimaryChangeError):
            set_primary_domain(db, "new.example.com", "old.example.com")
        assert primary_domain(db).name == "old.example.com"


def test_promotion_rejects_archive_migration(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert archive_configuration.acquire(blocking=False)
    try:
        with closing(open_db(cfg)) as db, pytest.raises(ArchiveMigrationInProgressError):
            set_primary_domain(db, "new.example.com", "old.example.com")
    finally:
        archive_configuration.release()


def test_promotion_rolls_back_every_write_on_failure(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "running", "running", container_id="container")
        db.execute(
            "CREATE TRIGGER reject_promotion BEFORE UPDATE OF is_primary ON domains "
            "WHEN NEW.name = 'new.example.com' AND NEW.is_primary = 1 "
            "BEGIN SELECT RAISE(ABORT, 'promotion failed'); END"
        )
        db.commit()
        with pytest.raises(Exception, match="promotion failed"):
            set_primary_domain(db, "new.example.com", "old.example.com")
        assert primary_domain(db).name == "old.example.com"
        assert db.execute("SELECT status FROM apps").fetchone()[0] == "running"
        assert db.execute("SELECT 1 FROM settings WHERE key = ?", (LEGACY_CERT_DOMAIN_KEY,)).fetchone() is None


def test_startup_recreates_promoted_apps_and_retries_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "running", "running", container_id="old-container")
        db.commit()
        set_primary_domain(db, "new.example.com", "old.example.com")

    attempts: list[str] = []
    thread_starts = 0

    class ImmediateThread:
        def __init__(self, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            self.target, self.args = target, args

        def start(self) -> None:
            nonlocal thread_starts
            thread_starts += 1
            if thread_starts > 1:
                self.target(*self.args)

    def restart(app_id: str, db: Any, config: Any) -> None:
        attempts.append(app_id)
        db.execute("UPDATE apps SET status = 'running' WHERE app_id = ?", (app_id,))
        db.commit()

    monkeypatch.setattr(startup.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(startup, "is_container_running", lambda _container: True)
    monkeypatch.setattr(startup, "image_exists", lambda _image: True)
    monkeypatch.setattr(startup, "restart_app_process", restart)

    startup.check_app_status(cfg)
    with closing(open_db(cfg)) as db:
        assert db.execute("SELECT status FROM apps").fetchone()[0] == "starting"
    startup.check_app_status(cfg)

    assert attempts == ["running-id"]
    with closing(open_db(cfg)) as db:
        assert db.execute("SELECT status FROM apps").fetchone()[0] == "running"


def test_startup_checks_the_promoted_primary_certificate_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    checked: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        start_mod,
        "get_cert_status",
        lambda cert_path, key_path: checked.append((cert_path, key_path)) or start_mod.CertStatus.OK,
    )

    with closing(open_db(cfg)) as db:
        set_primary_domain(db, "new.example.com", "old.example.com")
        asyncio.run(start_mod._ensure_tls_cert(cfg, db, object()))  # type: ignore[arg-type]

    assert checked == [
        (
            cfg.certs_dir / "new.example.com.pem",
            cfg.certs_dir / "new.example.com.key",
        )
    ]


def test_startup_skips_certificate_checks_for_http_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com", tls_enabled=True)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True, cert_status=DomainCertStatus.ACTIVE))
        set_primary_domain(db, "new.local", "old.example.com")
        monkeypatch.setattr(start_mod, "get_cert_status", lambda *_args: pytest.fail("unexpected check"))
        asyncio.run(start_mod._ensure_tls_cert(cfg, db, object()))  # type: ignore[arg-type]
