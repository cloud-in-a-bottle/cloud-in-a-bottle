# Launches the updater as its own transient systemd service so it survives the
# cgroup-wide SIGTERM from `systemctl restart openhost` (a plain child would be
# killed by the restart it is meant to cover).

from __future__ import annotations

import shutil
import subprocess
import sys
import time

from loguru import logger

from openhost_system_agent.updater.paths import ready_marker_path

_SCOPE_UNIT = "openhost-updater.service"

_READY_WAIT_SECONDS = 5.0
_READY_POLL = 0.05


def _systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


def _reset_stale_scope() -> None:
    try:
        subprocess.run(
            ["systemctl", "stop", _SCOPE_UNIT],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def stop_updater() -> None:
    """Stop the detached updater unit, releasing :443/:80. Best-effort, idempotent."""
    try:
        subprocess.run(["systemctl", "stop", _SCOPE_UNIT], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def launch_updater() -> bool:
    """Start the detached updater. Returns True if launched; never raises."""
    if not _systemd_run_available():
        logger.warning("systemd-run not found; skipping detached updater (update will still proceed)")
        return False

    _reset_stale_scope()

    marker = ready_marker_path()
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass

    cmd = [
        "systemd-run",
        f"--unit={_SCOPE_UNIT}",
        "--collect",
        sys.executable,
        # `-c` rather than `-m`: under -m the module loads as __main__ and cappa's
        # dispatch to `updater serve` exits without running the blocking server.
        "-c",
        "import sys; sys.argv=['openhost_system_agent','updater','serve']; "
        "from openhost_system_agent.cli import main; main()",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"failed to launch detached updater: {e}")
        return False

    if result.returncode != 0:
        logger.warning(f"systemd-run for updater exited {result.returncode}: {result.stderr.strip()}")
        return False

    # Wait (briefly) for the updater to reach its bind loop so the caller's
    # restart opens the downtime window with it already poised to grab 80/443.
    deadline = time.monotonic() + _READY_WAIT_SECONDS
    while time.monotonic() < deadline:
        if marker.exists():
            logger.info("detached updater launched and ready")
            return True
        time.sleep(_READY_POLL)

    logger.warning("detached updater launched but did not signal ready in time; proceeding with restart")
    return True
