from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import threading
from contextlib import closing
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from httpx import ConnectError
from hypercorn.config import Config as HypercornConfig
from litestar import Litestar
from litestar import get

from compute_space.core import startup
from compute_space.core.domains import PRIMARY_DOMAIN_RESTART_MARKER
from compute_space.core.domains import AppsBusyForPrimaryChangeError
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import primary_domain
from compute_space.core.domains import set_primary_domain
from compute_space.core.domains import upsert_record
from compute_space.core.operation_locks import operation_lock
from compute_space.core.updates import initialize_shutdown_event
from compute_space.core.updates import trigger_restart
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web import start as start_mod
from compute_space.web.exceptions import ConflictException
from compute_space.web.routes.api import archive_backend as archive_routes
from compute_space.web.routes.api import domains as domain_routes
from compute_space.web.routes.api.archive_backend import ConfigureArchiveRequest


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

        assert set_primary_domain(db, "new.example.com") is True
        assert primary_domain(db).name == "new.example.com"
        states = {row["name"]: row["status"] for row in db.execute("SELECT name, status FROM apps")}
        assert states == {
            "running": "starting",
            "stopped": "stopped",
            "live-error": "starting",
            "dead-error": "error",
        }
        claimed = {
            row["name"]: row["error_message"]
            for row in db.execute("SELECT name, error_message FROM apps WHERE status = 'starting'")
        }
        assert claimed == {"running": PRIMARY_DOMAIN_RESTART_MARKER, "live-error": PRIMARY_DOMAIN_RESTART_MARKER}


def test_promoting_current_domain_is_idempotent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "running", "running", container_id="container")
        db.commit()
        assert set_primary_domain(db, "old.example.com") is False
        assert primary_domain(db).name == "old.example.com"
        assert db.execute("SELECT status FROM apps").fetchone()[0] == "running"


@pytest.mark.parametrize("status", ["building", "starting", "removing"])
def test_promotion_rejects_active_app_operations(tmp_path: Path, status: str) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "busy", status)
        db.commit()
        with pytest.raises(AppsBusyForPrimaryChangeError):
            set_primary_domain(db, "new.example.com")
        assert primary_domain(db).name == "old.example.com"


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
            set_primary_domain(db, "new.example.com")
        assert primary_domain(db).name == "old.example.com"
        assert db.execute("SELECT status FROM apps").fetchone()[0] == "running"


def test_startup_completes_promoted_app_recovery_synchronously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        _app(db, "running", "running")
        _app(db, "stopped", "stopped")
        db.commit()
        set_primary_domain(db, "new.example.com")
        # A fast process restart can share a one-second timestamp with app creation.
        # The durable promotion marker must override the ordinary in-process guard.
        db.execute("UPDATE apps SET created_at = '9999-01-01 00:00:00' WHERE name = 'running'")
        db.commit()

    attempts: list[str] = []
    calling_thread = threading.get_ident()

    def restart(app_id: str, db: Any, config: Any) -> None:
        assert threading.get_ident() == calling_thread
        marker = db.execute("SELECT error_message FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
        assert marker == PRIMARY_DOMAIN_RESTART_MARKER
        attempts.append(app_id)
        db.execute("UPDATE apps SET status = 'running', error_message = NULL WHERE app_id = ?", (app_id,))
        db.commit()

    monkeypatch.setattr(startup, "is_container_running", lambda _container: True)
    monkeypatch.setattr(startup, "image_exists", lambda _image: True)
    monkeypatch.setattr(startup, "restart_app_process", restart)

    startup.check_app_status(cfg)

    assert attempts == ["running-id"]
    with closing(open_db(cfg)) as db:
        states = {row["name"]: row["status"] for row in db.execute("SELECT name, status FROM apps")}
        assert states == {"running": "running", "stopped": "stopped"}


def _add_local_domain(cfg: Any, name: str) -> None:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord(name, tls=False, mdns=True, cert_status=DomainCertStatus.ACTIVE))


@pytest.mark.asyncio
async def test_promotion_fails_fast_while_archive_worker_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com")
    _add_local_domain(cfg, "new.local")
    started = threading.Event()
    release = threading.Event()

    def configure(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(archive_routes.archive_backend, "configure_backend", configure)
    monkeypatch.setattr(domain_routes, "system_agent_reset_restart_limit_sync", pytest.fail)
    archive_db = open_db(cfg)
    promotion_db = open_db(cfg)
    archive_task = asyncio.create_task(
        archive_routes.configure_archive_backend.fn(
            ConfigureArchiveRequest("bucket", "key", "secret", s3_prefix="archive-1"), archive_db, cfg
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(ConflictException) as exc_info:
            await domain_routes.make_primary_domain.fn("new.local", cfg, promotion_db)
        assert exc_info.value.extra == {"code": "operation_busy"}
    finally:
        release.set()
        await archive_task
        archive_db.close()
        promotion_db.close()
    assert not operation_lock.locked()


@pytest.mark.asyncio
async def test_archive_fails_fast_while_promotion_worker_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com")
    _add_local_domain(cfg, "new.local")
    started = threading.Event()
    release = threading.Event()

    def reset_restart_limit() -> None:
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(domain_routes, "system_agent_reset_restart_limit_sync", reset_restart_limit)
    monkeypatch.setattr(domain_routes, "trigger_restart", lambda: None)
    promotion_db = open_db(cfg)
    archive_db = open_db(cfg)
    promotion_task = asyncio.create_task(domain_routes.make_primary_domain.fn("new.local", cfg, promotion_db))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(ConflictException) as exc_info:
            await archive_routes.configure_archive_backend.fn(
                ConfigureArchiveRequest("bucket", "key", "secret", s3_prefix="archive-1"), archive_db, cfg
            )
        assert exc_info.value.extra == {"code": "operation_busy"}
    finally:
        release.set()
        await promotion_task
        promotion_db.close()
        archive_db.close()
    assert not operation_lock.locked()


@pytest.mark.asyncio
async def test_concurrent_promotions_reject_the_second_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com")
    _add_local_domain(cfg, "first.local")
    _add_local_domain(cfg, "second.local")
    started = threading.Event()
    release = threading.Event()

    def reset_restart_limit() -> None:
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(domain_routes, "system_agent_reset_restart_limit_sync", reset_restart_limit)
    monkeypatch.setattr(domain_routes, "trigger_restart", lambda: None)
    first_db = open_db(cfg)
    second_db = open_db(cfg)
    first = asyncio.create_task(domain_routes.make_primary_domain.fn("first.local", cfg, first_db))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        with pytest.raises(ConflictException) as exc_info:
            await domain_routes.make_primary_domain.fn("second.local", cfg, second_db)
        assert exc_info.value.extra == {"code": "operation_busy"}
    finally:
        release.set()
        await first
        first_db.close()
        second_db.close()
    with closing(open_db(cfg)) as db:
        assert primary_domain(db).name == "first.local"


@pytest.mark.asyncio
async def test_promotion_sqlite_wait_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com")
    _add_local_domain(cfg, "new.local")
    blocker = sqlite3.connect(cfg.db_path)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(domain_routes, "system_agent_reset_restart_limit_sync", lambda: None)
    monkeypatch.setattr(domain_routes, "trigger_restart", lambda: None)
    request_db = open_db(cfg)
    task = asyncio.create_task(domain_routes.make_primary_domain.fn("new.local", cfg, request_db))
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
        heartbeat = asyncio.create_task(asyncio.sleep(0.01, result=True))
        assert await asyncio.wait_for(heartbeat, timeout=0.2)
    finally:
        blocker.commit()
        blocker.close()
        await task
        request_db.close()


@pytest.mark.asyncio
async def test_promotion_sets_shutdown_on_loop_before_unlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com")
    _add_local_domain(cfg, "new.local")
    shutdown_event = asyncio.Event()
    initialize_shutdown_event(shutdown_event)
    event_loop_thread = threading.get_ident()
    observed: list[bool] = []

    def trigger() -> None:
        assert threading.get_ident() == event_loop_thread
        assert operation_lock.locked()
        shutdown_event.set()
        observed.append(True)

    monkeypatch.setattr(domain_routes, "system_agent_reset_restart_limit_sync", lambda: None)
    monkeypatch.setattr(domain_routes, "trigger_restart", trigger)
    with closing(open_db(cfg)) as db:
        response = await domain_routes.make_primary_domain.fn("new.local", cfg, db)

    assert response.status_code == 200
    assert observed == [True]
    assert shutdown_event.is_set()
    assert not operation_lock.locked()
    with closing(open_db(cfg)) as archive_db, pytest.raises(ConflictException) as exc_info:
        await archive_routes.configure_archive_backend.fn(
            ConfigureArchiveRequest("bucket", "key", "secret", s3_prefix="archive-1"), archive_db, cfg
        )
    assert exc_info.value.extra == {"code": "restart_pending"}
    initialize_shutdown_event(asyncio.Event())


@pytest.mark.asyncio
async def test_promotion_rechecks_shutdown_after_acquiring_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="old.example.com")
    _add_local_domain(cfg, "new.local")
    checks = iter((False, True))
    monkeypatch.setattr(domain_routes, "is_shutdown_pending", lambda: next(checks))
    monkeypatch.setattr(
        domain_routes, "system_agent_reset_restart_limit_sync", lambda: pytest.fail("prepared restart")
    )

    with closing(open_db(cfg)) as db, pytest.raises(ConflictException) as exc_info:
        await domain_routes.make_primary_domain.fn("new.local", cfg, db)

    assert exc_info.value.extra == {"code": "restart_pending"}
    assert not operation_lock.locked()
    with closing(open_db(cfg)) as db:
        assert primary_domain(db).name == "old.example.com"


@pytest.mark.asyncio
async def test_hypercorn_flushes_response_when_handler_triggers_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    @get("/")
    async def restart() -> dict[str, bool]:
        trigger_restart()
        return {"flushed": True}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener_fd = listener.detach()

    config = HypercornConfig()
    config.bind = [f"fd://{listener_fd}"]
    config.graceful_timeout = 1
    config.shutdown_timeout = 1
    monkeypatch.setattr(start_mod, "cleanup_terminal_sessions", lambda: None)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    server = asyncio.create_task(start_mod._serve(Litestar(route_handlers=[restart]), config))
    response = None
    try:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            deadline = asyncio.get_running_loop().time() + 5
            while response is None:
                try:
                    response = await client.get("/", timeout=1)
                except ConnectError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise
                    await asyncio.sleep(0.01)
        assert response is not None
        assert response.status_code == 200
        assert response.json() == {"flushed": True}
        assert await asyncio.wait_for(server, timeout=3) is True
    finally:
        if not server.done():
            server.cancel()
            with pytest.raises(asyncio.CancelledError):
                await server
        with suppress(OSError):
            os.close(listener_fd)
        initialize_shutdown_event(asyncio.Event())


def test_startup_checks_the_promoted_primary_certificate_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    checked: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        start_mod,
        "get_cert_status",
        lambda cert_path, key_path: checked.append((cert_path, key_path)) or start_mod.CertStatus.OK,
    )

    with closing(open_db(cfg)) as db:
        set_primary_domain(db, "new.example.com")
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
        set_primary_domain(db, "new.local")
        monkeypatch.setattr(start_mod, "get_cert_status", lambda *_args: pytest.fail("unexpected check"))
        asyncio.run(start_mod._ensure_tls_cert(cfg, db, object()))  # type: ignore[arg-type]
