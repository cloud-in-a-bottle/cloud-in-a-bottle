from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing
from datetime import time
from pathlib import Path
from typing import Any

import attr
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

import compute_space.core.auto_suspend.status as status_mod
import compute_space.core.auto_update.runner as runner_mod
import compute_space.core.system_agent.client as client_mod
from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.auto_update.config import AutoUpdateConfig
from compute_space.core.auto_update.config import write_auto_update_config
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.routes.api.auto_suspend import auto_suspend_status

from .conftest import _make_test_config

_TOKEN = "host-status-token"


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Config]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    set_active_config(config)
    yield config


@pytest.fixture
def apt_running(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    running = [False]

    async def fake() -> bool:
        return running[0]

    monkeypatch.setattr(status_mod, "_apt_running", fake)
    return running


@pytest.fixture
def client(cfg: Config, apt_running: list[bool]) -> Iterator[TestClient[Litestar]]:
    app = Litestar(
        route_handlers=[auto_suspend_status],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )
    with TestClient(app=app) as test_client:
        yield test_client


def _configure_token(cfg: Config) -> None:
    set_active_config(attr.evolve(cfg, auto_suspend_status_token=_TOKEN))


def _get_status(client: TestClient[Litestar], token: str | None = _TOKEN) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/api/auto-suspend-status", headers=headers)


# ─────────────── auth ───────────────


def test_not_served_without_a_configured_token(client: TestClient[Litestar]) -> None:
    assert _get_status(client).status_code == 404


def test_rejects_a_missing_token(client: TestClient[Litestar], cfg: Config) -> None:
    _configure_token(cfg)
    assert _get_status(client, token=None).status_code == 401


def test_rejects_a_wrong_token(client: TestClient[Litestar], cfg: Config) -> None:
    _configure_token(cfg)
    assert _get_status(client, token="not-it").status_code == 401


# ─────────────── status ───────────────


def test_idle_instance_has_no_busy_reasons(client: TestClient[Litestar], cfg: Config) -> None:
    _configure_token(cfg)
    resp = _get_status(client)
    assert resp.status_code == 200
    assert resp.json()["busy_reasons"] == []


def test_reports_os_package_updates(client: TestClient[Litestar], cfg: Config, apt_running: list[bool]) -> None:
    _configure_token(cfg)
    apt_running[0] = True
    assert _get_status(client).json()["busy_reasons"] == ["os_packages"]


def test_reports_apps_building(client: TestClient[Litestar], cfg: Config) -> None:
    _configure_token(cfg)
    with closing(sqlite3.connect(cfg.db_path)) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('buildingappid', 'building-app', '0.1.0', '/tmp/building-app', 19124, 'building')"
        )
        db.commit()
    assert _get_status(client).json()["busy_reasons"] == ["apps"]


@pytest.mark.asyncio
async def test_reports_a_platform_update_being_started(cfg: Config, apt_running: list[bool]) -> None:
    await client_mod.apply_lock.acquire()
    try:
        with closing(get_db()) as db:
            status = await status_mod.get_auto_suspend_status(db)
    finally:
        client_mod.apply_lock.release()
    assert status.busy_reasons == (status_mod.BusyReason.PLATFORM_UPDATE,)


def test_reports_the_auto_update_time(client: TestClient[Litestar], cfg: Config) -> None:
    _configure_token(cfg)
    with closing(get_db()) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=True, time_utc=time(9, 30)))
    assert _get_status(client).json()["auto_update_time_utc"] == "09:30:00"


def test_no_auto_update_time_when_auto_updates_are_off(client: TestClient[Litestar], cfg: Config) -> None:
    _configure_token(cfg)
    with closing(get_db()) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=False, time_utc=time(9, 30)))
    assert _get_status(client).json()["auto_update_time_utc"] is None


@pytest.mark.asyncio
async def test_reports_a_scheduled_update_checking_for_a_release(
    cfg: Config, apt_running: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "_scheduled_run_in_progress", True)
    with closing(get_db()) as db:
        status = await status_mod.get_auto_suspend_status(db)
    assert status.busy_reasons == (status_mod.BusyReason.PLATFORM_UPDATE,)


# ─────────────── apt units ───────────────


def _fake_systemctl(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["systemctl", "is-active", "apt-daily.service", "apt-daily-upgrade.service"]
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(status_mod.subprocess, "run", run)


@pytest.mark.asyncio
async def test_apt_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_systemctl(monkeypatch, "inactive\ninactive\n", returncode=3)
    assert not await status_mod._apt_running()


@pytest.mark.asyncio
async def test_apt_upgrade_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # Oneshot units sit in "activating" while they run.
    _fake_systemctl(monkeypatch, "inactive\nactivating\n", returncode=0)
    assert await status_mod._apt_running()


@pytest.mark.asyncio
async def test_apt_unknown_state_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_systemctl(monkeypatch, "", returncode=1)
    with pytest.raises(RuntimeError):
        await status_mod._apt_running()
