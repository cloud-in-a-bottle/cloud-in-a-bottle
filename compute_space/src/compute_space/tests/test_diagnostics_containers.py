"""Live-podman tests for the diagnostics collectors.

These exercise the diagnostics code against a REAL rootless-podman container
(no subprocess mocking), so they catch drift in podman's actual output shapes
that the mocked unit tests in ``test_diagnostics.py`` can't. They are gated
behind the ``requires_containers`` marker and only run with
``pytest --run-containers`` on a host with a working rootless podman.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest

from compute_space.core import diagnostics
from compute_space.core.containers import is_container_running

requires_containers = pytest.mark.requires_containers

# A small image that's already used by the test-app fixtures, so it's available
# in the container CI env without an extra pull.
_IMAGE = "python:3.12-alpine"
_PODMAN_TIMEOUT = 120


def _podman(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=_PODMAN_TIMEOUT, check=check)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_running(container_id: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_container_running(container_id):
            return
        time.sleep(0.5)
    raise AssertionError(f"container {container_id} did not reach running within {timeout}s")


def _wait_http_ready(port: int, timeout: float = 30.0) -> None:
    """Wait until the container's in-process http.server has actually bound the
    port (a container reporting 'running' doesn't mean the server is serving
    yet). Probes '/' until the health check reports the app up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        h = asyncio.run(diagnostics._collect_app_health(port, "/"))
        if h.checked and h.healthy:
            return
        time.sleep(0.5)
    raise AssertionError(f"http server on :{port} did not become ready within {timeout}s")


@pytest.fixture
def http_container() -> Iterator[tuple[str, int]]:
    """Start a real container serving HTTP on a mapped loopback port; yield
    ``(container_id, host_port)`` and force-remove it afterwards."""
    port = _free_port()
    name = f"oh-diag-test-{uuid.uuid4().hex[:8]}"
    # Serve /tmp on :8000 inside the container; map to a free host loopback port.
    proc = _podman(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:8000",
        "--memory=128m",
        _IMAGE,
        "python",
        "-m",
        "http.server",
        "8000",
        "--directory",
        "/tmp",
    )
    container_id = proc.stdout.strip()
    try:
        _wait_running(container_id)
        yield container_id, port
    finally:
        _podman("rm", "-f", name, check=False)


# ─── container runtime ───────────────────────────────────────────────────────


@requires_containers
def test_container_runtime_reports_real_podman() -> None:
    rt = diagnostics._collect_container_runtime()
    assert rt.available is True
    assert rt.error is None
    # A real version string like "5.8.3".
    assert rt.version and rt.version[0].isdigit()
    # Cloud in a Bottle's test/CI podman is rootless.
    assert rt.rootless is True


# ─── is_container_running ──────────────────────────────────────────────────


@requires_containers
def test_is_container_running_true_then_false(http_container: tuple[str, int]) -> None:
    container_id, _ = http_container
    assert is_container_running(container_id) is True
    _podman("stop", "-t", "1", container_id, check=False)
    # Poll until podman reports it stopped.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and is_container_running(container_id):
        time.sleep(0.5)
    assert is_container_running(container_id) is False


@requires_containers
def test_is_container_running_false_for_unknown() -> None:
    assert is_container_running("does-not-exist-" + uuid.uuid4().hex) is False


# ─── app resources (podman stats) ────────────────────────────────────────────


@requires_containers
def test_app_resources_running_reports_live_usage(http_container: tuple[str, int]) -> None:
    container_id, _ = http_container
    r = diagnostics._collect_app_resources(container_id, cpu_cores_limit=1.0, memory_mb_limit=128)
    assert r.running is True
    assert r.error is None
    # Manifest limits are echoed back.
    assert r.cpu_cores_limit == 1.0
    assert r.memory_mb_limit == 128
    # Live memory usage was parsed from real podman stats output.
    assert r.memory_usage_bytes is not None and r.memory_usage_bytes > 0
    # CPU percent is a real (possibly 0.0) number, not None.
    assert r.cpu_percent is not None


@requires_containers
def test_app_resources_stopped_container_not_running(http_container: tuple[str, int]) -> None:
    """A stopped container reports running=False with no misleading zero stats,
    while still echoing the manifest limits (regression: podman stats emits a
    zero-valued entry for exited containers)."""
    container_id, _ = http_container
    _podman("stop", "-t", "1", container_id, check=False)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and is_container_running(container_id):
        time.sleep(0.5)
    r = diagnostics._collect_app_resources(container_id, cpu_cores_limit=0.5, memory_mb_limit=64)
    assert r.running is False
    assert r.cpu_percent is None
    assert r.memory_usage_bytes is None
    assert r.cpu_cores_limit == 0.5
    assert r.memory_mb_limit == 64


# ─── app health (loopback HTTP probe) ────────────────────────────────────────


@requires_containers
def test_app_health_healthy_against_real_server(http_container: tuple[str, int]) -> None:

    _, port = http_container
    _wait_http_ready(port)
    health = asyncio.run(diagnostics._collect_app_health(port, "/"))
    assert health.checked is True
    assert health.healthy is True
    assert health.status_code is not None and health.status_code < 500
    assert health.checked_path == "/"


@requires_containers
def test_app_health_unhealthy_on_dead_port() -> None:

    # Nothing listening on this free port -> connection refused -> unhealthy.
    health = asyncio.run(diagnostics._collect_app_health(_free_port(), "/"))
    assert health.checked is True
    assert health.healthy is False
    assert health.status_code is None
    assert health.error


@requires_containers
def test_app_health_custom_path_normalized(http_container: tuple[str, int]) -> None:

    _, port = http_container
    _wait_http_ready(port)
    # A missing-leading-slash path is normalized; a 404 is still "healthy"
    # (any status < 500 means the app is up and answering).
    health = asyncio.run(diagnostics._collect_app_health(port, "healthz"))
    assert health.checked_path == "/healthz"
    assert health.checked is True
    # http.server returns 404 for /healthz, which is < 500 -> healthy.
    assert health.healthy is True
    assert health.status_code == 404
