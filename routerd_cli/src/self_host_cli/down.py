"""``openhost down`` -- stop the Cloud in a Bottle router cleanly."""

import argparse
import os
import signal
import time

_PID_DIR = os.path.expanduser("~/.openhost/run")
_ROUTER_PID = os.path.join(_PID_DIR, "router.pid")


def _read_pid(path: str) -> int | None:
    """Read a PID from a pidfile, or None if missing/invalid."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it


def _stop_process(name: str, pid_path: str) -> None:
    """Stop a process by its pidfile. Sends SIGTERM, waits, then SIGKILL."""
    pid = _read_pid(pid_path)
    if pid is None:
        print(f"  {name}: no pidfile found, skipping.")
        return

    if not _is_alive(pid):
        print(f"  {name}: process {pid} not running.")
        _cleanup_pidfile(pid_path)
        return

    print(f"  {name}: stopping (pid {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_pidfile(pid_path)
        return

    # Wait up to 10s for graceful shutdown
    deadline = time.time() + 10
    while time.time() < deadline and _is_alive(pid):
        time.sleep(0.5)

    if _is_alive(pid):
        print(f"  {name}: still running, sending SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    _cleanup_pidfile(pid_path)
    print(f"  {name}: stopped.")


def _cleanup_pidfile(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def run_down(_args: argparse.Namespace) -> None:
    print("Stopping Cloud in a Bottle...")
    _stop_process("Router", _ROUTER_PID)
    print()
    print("Cloud in a Bottle stopped.")
