"""Tests for the ``/api/drop-docker-cache`` endpoint and its backing helper.

The endpoint removes leftover build containers, then runs
``podman image prune --all --force``, via ``drop_docker_build_cache``
in compute_space.core.containers.
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


_APP_CONTAINER = "a" * 64
_BUILD_CONTAINER_1 = "b" * 64
_BUILD_CONTAINER_2 = "c" * 64


class _FakePodman:
    def __init__(
        self,
        build_containers: list[str],
        *,
        prune_returncode: int = 0,
        ps_external_returncode: int = 0,
        rm_returncode: int = 0,
    ) -> None:
        self.build_containers = build_containers
        self.prune_returncode = prune_returncode
        self.ps_external_returncode = ps_external_returncode
        self.rm_returncode = rm_returncode
        self.cmds: list[list[str]] = []

    def run(self, cmd: list[str], capture_output: bool, text: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        self.cmds.append(cmd)
        if cmd == ["podman", "ps", "--all", "--quiet", "--no-trunc"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{_APP_CONTAINER}\n", stderr="")
        if cmd == ["podman", "ps", "--all", "--quiet", "--no-trunc", "--external"]:
            if self.ps_external_returncode != 0:
                return subprocess.CompletedProcess(cmd, self.ps_external_returncode, stdout="", stderr="ps error")
            ids = [_APP_CONTAINER, *self.build_containers]
            return subprocess.CompletedProcess(cmd, 0, stdout="".join(f"{i}\n" for i in ids), stderr="")
        if cmd[:3] == ["podman", "rm", "--force"]:
            return subprocess.CompletedProcess(cmd, self.rm_returncode, stdout="", stderr="rm error")
        if cmd == ["podman", "image", "prune", "--all", "--force"]:
            assert timeout == 120
            if self.prune_returncode != 0:
                return subprocess.CompletedProcess(
                    cmd, self.prune_returncode, stdout="", stderr="podman image prune error"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="Total reclaimed space: 12.3MB\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")


def test_drop_docker_build_cache_runs_podman_image_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    podman = _FakePodman(build_containers=[])
    monkeypatch.setattr(subprocess, "run", podman.run)

    output = drop_docker_build_cache()

    assert output == "Total reclaimed space: 12.3MB"
    assert [c for c in podman.cmds if c[:2] == ["podman", "rm"]] == []
    assert podman.cmds[-1] == ["podman", "image", "prune", "--all", "--force"]


def test_drop_docker_build_cache_removes_leftover_build_containers_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    podman = _FakePodman(build_containers=[_BUILD_CONTAINER_2, _BUILD_CONTAINER_1])
    monkeypatch.setattr(subprocess, "run", podman.run)

    drop_docker_build_cache()

    rm_cmds = [c for c in podman.cmds if c[:2] == ["podman", "rm"]]
    assert rm_cmds == [["podman", "rm", "--force", _BUILD_CONTAINER_1, _BUILD_CONTAINER_2]]
    assert podman.cmds.index(rm_cmds[0]) < podman.cmds.index(["podman", "image", "prune", "--all", "--force"])


def test_drop_docker_build_cache_raises_when_listing_build_containers_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    podman = _FakePodman(build_containers=[_BUILD_CONTAINER_1], ps_external_returncode=1)
    monkeypatch.setattr(subprocess, "run", podman.run)

    with pytest.raises(RuntimeError, match="ps error"):
        drop_docker_build_cache()
    assert [c for c in podman.cmds if c[:2] == ["podman", "rm"] or c[:3] == ["podman", "image", "prune"]] == []


def test_drop_docker_build_cache_raises_when_removing_build_containers_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    podman = _FakePodman(build_containers=[_BUILD_CONTAINER_1], rm_returncode=1)
    monkeypatch.setattr(subprocess, "run", podman.run)

    with pytest.raises(RuntimeError, match="rm error"):
        drop_docker_build_cache()
    assert ["podman", "image", "prune", "--all", "--force"] not in podman.cmds


def test_drop_docker_build_cache_raises_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    podman = _FakePodman(build_containers=[], prune_returncode=1)
    monkeypatch.setattr(subprocess, "run", podman.run)

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
    # Litestar's default 500 handler masks `detail`, but `extra` passes through
    # unmasked, so the real podman output is carried there instead.
    body = resp.json()
    assert body["detail"] == "Internal Server Error"
    assert body["extra"] == {"output": "podman engine error"}
