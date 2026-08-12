"""Tests for the ``/api/drop-docker-cache`` endpoint and its backing helper.

The endpoint runs ``podman image prune --all --force`` via
``drop_docker_build_cache`` in compute_space.core.containers.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.containers import drop_docker_build_cache
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api import system as system_routes
from compute_space.web.routes.api.system import system_routes as system_router


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20800)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(system_router)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def test_drop_docker_build_cache_runs_podman_image_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        calls["cmd"] = cmd
        calls["capture_output"] = capture_output
        calls["text"] = text
        calls["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="Total reclaimed space: 12.3MB\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = drop_docker_build_cache()

    assert output == "Total reclaimed space: 12.3MB"
    assert calls["cmd"] == ["podman", "image", "prune", "--all", "--force"]
    assert calls["capture_output"] is True
    assert calls["text"] is True
    assert calls["timeout"] == 120


def test_drop_docker_build_cache_raises_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="podman image prune error",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="podman image prune error"):
        drop_docker_build_cache()


def test_drop_docker_cache_endpoint_success(
    monkeypatch: pytest.MonkeyPatch, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    monkeypatch.setattr(
        system_routes,
        "drop_docker_build_cache",
        lambda: "Total reclaimed space: 12.3MB",
    )
    client.cookies.update(cookies)
    resp = client.post("/api/drop-docker-cache")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "output": "Total reclaimed space: 12.3MB"}


def test_drop_docker_cache_endpoint_failure(
    monkeypatch: pytest.MonkeyPatch, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    def _raise_error() -> str:
        raise RuntimeError("podman engine error")

    monkeypatch.setattr(system_routes, "drop_docker_build_cache", _raise_error)
    client.cookies.update(cookies)
    resp = client.post("/api/drop-docker-cache")
    assert resp.status_code == 500
    # Litestar's default 500 handler masks the real detail; see app.py's
    # app-wide `_log_unhandled_exception` handler, which this router now defers to.
    assert resp.json()["detail"] == "Internal Server Error"
