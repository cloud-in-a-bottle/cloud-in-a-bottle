"""Route-level tests for the ``/app_logs_stream/<app_id>`` WebSocket endpoint.

Drives the real WebSocket layer (inline auth check, route wiring, the
build-log-then-container-follow handoff) via TestClient; podman is faked so no
daemon is required.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient

from compute_space.core import log_stream
from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20200)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    # The WebSocket handshake is a plain HTTP GET, so the session cookie rides in
    # a Cookie header just like a normal request.
    return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def _seed_running_app(cfg: Any, name: str, container_id: str | None, port: int) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, container_id)
               VALUES (?, ?, '1.0', ?, ?, 'running', ?)""",
            (app_id, name, f"/repo/{name}", port, container_id),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _write_build_log(cfg: Any, name: str, text: str) -> None:
    path = os.path.join(cfg.temporary_data_dir, "app_temp_data", name, "docker.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _fake_follow(lines: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(container_id: str, tail_lines: int) -> AsyncIterator[str]:
        for line in lines:
            yield line

    monkeypatch.setattr(log_stream, "_podman_follow", fake)


def test_stream_requires_auth(cfg: Any, client: TestClient[Litestar]) -> None:
    app_id = _seed_running_app(cfg, "notes", "cid1", 20210)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/app_logs_stream/{app_id}") as ws:  # no cookie
            ws.receive_text()
    assert exc.value.code == 4401


def test_stream_unknown_app_closes(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/app_logs_stream/{new_app_id()}", headers=_cookie_header(cookies)) as ws:
            ws.receive_text()
    assert exc.value.code == 4404


def test_stream_emits_build_log_then_container_lines(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_build_log(cfg, "notes", "build line 1\nbuild line 2\n")
    _fake_follow(["container line a", "container line b"], monkeypatch)
    app_id = _seed_running_app(cfg, "notes", "cid1", 20211)

    msgs = []
    with client.websocket_connect(f"/app_logs_stream/{app_id}", headers=_cookie_header(cookies)) as ws:
        for _ in range(4):
            msgs.append(ws.receive_text())

    assert msgs == [
        "build line 1\nbuild line 2",
        "=== Container logs ===",
        "container line a",
        "container line b",
    ]


def test_stream_no_container_emits_build_only(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_build_log(cfg, "notes", "only a build log\n")
    _fake_follow(["should not appear"], monkeypatch)
    app_id = _seed_running_app(cfg, "notes", None, 20212)  # no container

    with client.websocket_connect(f"/app_logs_stream/{app_id}", headers=_cookie_header(cookies)) as ws:
        first = ws.receive_text()

    assert first == "only a build log"
