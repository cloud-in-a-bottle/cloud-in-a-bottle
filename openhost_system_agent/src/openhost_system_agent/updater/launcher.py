# Launch the updater in its OWN systemd scope (systemd-run --scope) so it lands
# in a separate cgroup. openhost.service uses KillMode=control-group, so a plain
# child of compute_space would be SIGTERM'd by the very `systemctl restart
# openhost` it is meant to cover; a separate scope survives it. Runs as root
# (reached via `sudo openhost_system_agent` from compute_space).

from __future__ import annotations

import shutil
import subprocess
import sys
import time

from loguru import logger

from openhost_system_agent.updater.paths import ready_marker_path

# A stable unit name so a second launch can't stack duplicate updaters; we reset
# any lingering one first. Random enough to avoid clashing with real units.
_SCOPE_UNIT = "openhost-updater.scope"

# How long to wait for the launched updater to reach its bind loop (touch the
# ready marker) before returning. Bounds the head start we give it so a failed
# launch can't stall the restart for long.
_READY_WAIT_SECONDS = 5.0
_READY_POLL = 0.05


def _systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


def _reset_stale_scope() -> None:
    """Best-effort: stop a leftover updater scope from a prior/aborted run.

    A previous updater that exited cleanly already removed its scope; this only
    matters if one is somehow still around, which would make the new
    ``systemd-run`` fail with "unit already exists".
    """
    try:
        subprocess.run(
            ["systemctl", "stop", _SCOPE_UNIT],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def launch_updater() -> bool:
    """Start the detached updater. Returns True if it was launched.

    Never raises: a failure to launch the (cosmetic) updater must never abort or
    delay the actual update. The caller proceeds with the restart regardless.
    """
    if not _systemd_run_available():
        logger.warning("systemd-run not found; skipping detached updater (update will still proceed)")
        return False

    _reset_stale_scope()

    # Clear any stale ready marker so our wait below observes THIS launch reach
    # its bind loop, not a previous run's marker.
    marker = ready_marker_path()
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass

    cmd = [
        "systemd-run",
        f"--unit={_SCOPE_UNIT}",
        "--scope",
        # Don't let the scope inherit openhost.service's cgroup/slice — it must
        # be independent so the restart doesn't tear it down.
        "--collect",
        sys.executable,
        "-m",
        "openhost_system_agent.cli",
        "updater",
        "serve",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"failed to launch detached updater: {e}")
        return False

    if result.returncode != 0:
        logger.warning(f"systemd-run for updater exited {result.returncode}: {result.stderr.strip()}")
        return False

    # Wait for the updater to reach its bind loop (ready marker) before returning,
    # so the caller's restart opens the downtime window with the updater already
    # poised to grab 80/443 rather than still importing Python. Bounded so a
    # non-starting updater can't stall the restart.
    deadline = time.monotonic() + _READY_WAIT_SECONDS
    while time.monotonic() < deadline:
        if marker.exists():
            logger.info("detached updater launched and ready")
            return True
        time.sleep(_READY_POLL)

    logger.warning("detached updater launched but did not signal ready in time; proceeding with restart")
    return True
