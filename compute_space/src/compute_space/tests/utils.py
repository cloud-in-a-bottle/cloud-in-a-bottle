"""Shared test utilities for process and network helpers.

Importable from both top-level tests/ and compute_space/tests/ since
the compute_space package is installed.
"""

import atexit
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import closing
from contextlib import contextmanager
from typing import IO
from typing import Any

import pytest
import requests

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from compute_space.config import Config
from compute_space.core.domains import Domain
from compute_space.core.domains import primary_domain_or_none


def write_first_boot_beside(config_path: str, primary: Domain | None) -> None:
    """Write a ``first_boot.toml`` next to ``config_path`` seeding ``primary``, so a router
    subprocess (whose config.toml no longer carries the domain) boots with one."""
    if primary is None:
        return
    fb_path = os.path.join(os.path.dirname(config_path), "first_boot.toml")
    with open(fb_path, "w") as f:
        f.write(f'domain = "{primary.name}"\ntls = {"true" if primary.tls else "false"}\n')


def make_router_env(config_path: str) -> dict[str, str]:
    """Subprocess env for launching the router against a generated config."""
    return {**os.environ, "OPENHOST_ROUTER_CONFIG": config_path}


def kill_tree(proc: subprocess.Popen[Any], sig: int = signal.SIGTERM) -> None:
    """Kill a process and its children via process group."""
    try:
        os.killpg(proc.pid, sig)
    except (PermissionError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def port_connectable(host: str, port: int, timeout: float = 1) -> bool:
    """Check if a TCP connection can be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def poll(fn: Any, timeout: float, interval: float = 5, fail_msg: str = "Polling timed out") -> Any:
    """Call *fn* repeatedly until it returns a truthy value or *timeout* expires."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
        except Exception as e:
            last_exc = e
        time.sleep(interval)
    extra = f" (last exception: {last_exc})" if last_exc else ""
    pytest.fail(f"{fail_msg}{extra}")


def app_id_for(session: requests.Session, router_url: str, app_name: str) -> str | None:
    """Look up an app's app_id by name via ``GET /api/apps``. None if not deployed."""
    r = session.get(f"{router_url}/api/apps", timeout=10)
    if r.status_code != 200:
        return None
    for entry in r.json():
        if entry.get("name") == app_name:
            return str(entry["app_id"])
    return None


def wait_app_running(session: requests.Session, router_url: str, app_name: str, timeout: float = 300) -> None:
    """Poll ``/api/app_status/<app_id>`` until the app reports *running*.

    Resolves ``app_name`` -> app_id on each poll so we can wait through the
    initial deploy window when the row may not exist yet.
    """

    def _check() -> bool:
        app_id = app_id_for(session, router_url, app_name)
        if app_id is None:
            return False
        r = session.get(f"{router_url}/api/app_status/{app_id}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data["status"] == "error":
                pytest.fail(f"{app_name} deploy failed: {data.get('error')}")
            return bool(data["status"] == "running")
        return False

    poll(_check, timeout=timeout, interval=5, fail_msg=f"{app_name} did not reach 'running' state")


def wait_app_removed(session: requests.Session, router_url: str, app_name: str, timeout: float = 120) -> None:
    """Poll ``/api/apps`` until ``app_name`` is gone.

    /remove_app returns 202 and runs teardown in a background thread;
    tests asserting on filesystem / container state after a remove
    must wait for the row to disappear (the row is the last step).
    """

    def _check() -> bool:
        app_id = app_id_for(session, router_url, app_name)
        if app_id is None:
            return True
        # Surface persistent error states immediately so the poll doesn't
        # silently spin until timeout when remove_app_background gave up.
        r = session.get(f"{router_url}/api/app_status/{app_id}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "error":
                pytest.fail(f"{app_name} removal failed: {data.get('error')}")
        return False

    poll(_check, timeout=timeout, interval=2, fail_msg=f"{app_name} was not removed within {timeout}s")


def router_cmd() -> list[str]:
    """Return the command list to launch the router as a subprocess.

    Uses ``sys.executable`` rather than shelling out through pixi: the
    test runner is itself launched via ``pixi run -e dev pytest``, so
    ``sys.executable`` already points at the dev env's Python and
    ``compute_space`` is importable as the editable package install.
    """
    return [sys.executable, "-m", "compute_space"]


# ---------------------------------------------------------------------------
# Router process context manager
# ---------------------------------------------------------------------------

# Global so atexit can clean up if pytest crashes before teardown runs.
_router_proc: subprocess.Popen[bytes] | None = None


def _kill_router() -> None:
    if _router_proc and _router_proc.poll() is None:
        kill_tree(_router_proc)


atexit.register(_kill_router)


@contextmanager
def managed_router(config: Config, startup_timeout: int = 30) -> Generator[subprocess.Popen[bytes], None, None]:
    """Start a router subprocess, wait for /health, yield it, then tear down.

    Registers an atexit handler so the process is killed even if pytest crashes.
    Logs stdout/stderr to ``{config.temporary_data_dir}/router.log``.
    """
    global _router_proc

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((config.host, config.port)) == 0:
            raise RuntimeError(f"Port {config.port} already in use — kill the stale process")

    config_path = os.path.join(config.temporary_data_dir, "config.toml")
    config.to_toml(config_path)
    with closing(sqlite3.connect(config.db_path)) as db:
        db.row_factory = sqlite3.Row
        write_first_boot_beside(config_path, primary_domain_or_none(db))
    env = make_router_env(config_path)

    log_path = os.path.join(config.temporary_data_dir, "router.log")
    log_file: IO[str] = open(log_path, "w")
    base_url = f"http://{config.host}:{config.port}"

    try:
        proc = subprocess.Popen(
            router_cmd(),
            cwd=str(COMPUTE_SPACE_PACKAGE_DIR),
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        _router_proc = proc
    except Exception:
        log_file.close()
        raise

    # Wait for /health
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=1)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.3)
    else:
        kill_tree(proc)
        proc.wait()
        log_file.close()
        with open(log_path) as f:
            log_content = f.read()
        raise RuntimeError(f"Router failed to start.\nlog: {log_content}")

    try:
        yield proc
    finally:
        try:
            kill_tree(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                proc.wait()
        finally:
            _router_proc = None
            log_file.close()
