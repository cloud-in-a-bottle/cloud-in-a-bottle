"""Root conftest – opt-in flags for heavy integration tests.

By default only lightweight tests run (no external runtimes needed).
Pass flags to opt in to heavier suites:

    pixi run -e dev pytest                      # local-only tests
    pixi run -e dev pytest --run-containers     # + Podman integration tests
    pixi run -e dev pytest --run-tls            # + TLS cert tests
"""

import os
import shutil
import subprocess
import sys

import pytest

from compute_space.tests.router_entrypoint import TEST_CONTAINER_PREFIX_ENV
from compute_space.tests.router_entrypoint import apply_test_patches

# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--run-containers",
        action="store_true",
        default=False,
        help="Run tests that require a working rootless podman setup.",
    )
    parser.addoption(
        "--run-tls",
        action="store_true",
        default=False,
        help="Run TLS cert tests that require pebble and coredns.",
    )


# ---------------------------------------------------------------------------
# Tool availability checks
# ---------------------------------------------------------------------------


def _container_runtime_available():
    """Gate for --run-containers tests: is podman *usable* on this host?

    Runs ``podman info`` (not just ``--version``) because the
    ``requires_containers`` tests need a working rootless namespace, not
    just the binary on PATH.
    """
    try:
        r = subprocess.run(["podman", "info"], capture_output=True, timeout=10)
        if r.returncode == 0:
            return True
        if sys.platform == "darwin":
            # On macOS, podman runs inside a VM that may be stopped or have a
            # stale socket. Try to (re)start it before giving up.
            print("podman info failed; attempting `podman machine start`...", file=sys.stderr)
            subprocess.run(["podman", "machine", "start"], capture_output=True, timeout=120)
            r = subprocess.run(["podman", "info"], capture_output=True, timeout=10)
            return r.returncode == 0
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # OSError covers odd failure modes (EPERM on the binary, fd
        # exhaustion, etc.) that would otherwise crash pytest
        # collection rather than gracefully skip --run-containers tests.
        return False


def _pebble_available():
    return shutil.which("pebble") is not None


def _coredns_available():
    return shutil.which("coredns") is not None


# ---------------------------------------------------------------------------
# Auto-skip logic
# ---------------------------------------------------------------------------


def pytest_configure(config):
    # Podman container and image names are host-global, but every test router has
    # its own DB, so two routers running at once under pytest-xdist would replace or
    # delete each other's same-named apps (e.g. both deploying default `secrets`).
    # Give each worker its own name prefix: router subprocesses pick it up from the
    # env in compute_space.tests.router_entrypoint, and this process is patched
    # directly.  Only for container runs, so unit tests asserting the default
    # prefix are unaffected.
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker and config.getoption("--run-containers"):
        os.environ[TEST_CONTAINER_PREFIX_ENV] = f"openhost-{worker}-"
        apply_test_patches()


def pytest_collection_modifyitems(config, items):
    run_containers = config.getoption("--run-containers")
    run_tls = config.getoption("--run-tls")

    if run_containers and not _container_runtime_available():
        raise RuntimeError("--run-containers flag passed but podman does not seem to be available")

    if run_tls and not _pebble_available():
        raise RuntimeError("--run-tls flag passed but pebble is not in PATH")
    if run_tls and not _coredns_available():
        raise RuntimeError("--run-tls flag passed but coredns is not in PATH")

    for item in items:
        if "requires_containers" in item.keywords:
            if not run_containers:
                item.add_marker(pytest.mark.skip(reason="needs --run-containers flag to run"))

        if "requires_tls" in item.keywords:
            if not run_tls:
                item.add_marker(pytest.mark.skip(reason="needs --run-tls flag to run"))
