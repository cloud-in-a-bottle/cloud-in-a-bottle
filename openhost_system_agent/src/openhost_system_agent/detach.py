"""Runs the apply walk as its own transient systemd unit.

compute_space starts the apply, so it inherits openhost.service's cgroup and
`systemctl stop openhost` would kill the walk mid-flight. Re-launching out of that
cgroup is what makes stopping the service possible at all.

ExecStopPost is the failsafe: however the walk ends, systemd still starts openhost,
so no exit path can leave the instance stopped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from loguru import logger

from openhost_system_agent.updater.paths import DATA_DIR_ENV

OPENHOST_UNIT = "openhost.service"
APPLY_UNIT = "openhost-apply.service"
DETACHED_ENV = "OPENHOST_APPLY_DETACHED"

_WAIT_TIMEOUT_SECONDS = 3600.0
_RUNTIME_MAX_SECONDS = 3600
_ACTIVE_STATES = ("active", "activating", "deactivating", "reloading")

# `-c` rather than `-m`: under -m the module loads as __main__ and cappa's dispatch
# exits without running the command.
_ENTRYPOINT = (
    "import sys; sys.argv=['openhost_system_agent','update','apply']; "
    "from openhost_system_agent.cli import main; main()"
)


class ApplyAlreadyRunningError(RuntimeError):
    """A previous apply is still running as APPLY_UNIT."""


def _systemctl(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)


def is_detached() -> bool:
    return os.environ.get(DETACHED_ENV) == "1"


def apply_is_running() -> bool:
    # `show`, not `is-active`: once --collect has reaped the unit, asking for it by
    # name makes PID 1 log a failed transient-file open on every call.
    try:
        state = _systemctl("show", "--property=ActiveState", "--value", APPLY_UNIT, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False  # can't tell; systemd-run's own name collision is the backstop
    return state.strip() in _ACTIVE_STATES


def wait_for_apply(timeout: float = _WAIT_TIMEOUT_SECONDS, poll: float = 2.0) -> bool:
    """Block until the apply unit is gone. False if it outlived ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not apply_is_running():
            return True
        time.sleep(poll)
    return False


def detach_apply() -> None:
    """Start the walk as APPLY_UNIT, raising if one is already in flight.

    No inline fallback when systemd-run is missing: stopping a service that
    nothing can start again is worse than refusing to update.
    """
    if shutil.which("systemd-run") is None:
        raise RuntimeError(
            "systemd-run is not available, so the apply cannot be detached from openhost.service. "
            "Re-running the ansible deploy will reinstall the expected host tooling."
        )
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    cmd = [
        "systemd-run",
        f"--unit={APPLY_UNIT}",
        "--description=Cloud in a Bottle update apply",
        "--collect",
        # openhost is down for the whole walk, so bound it: on expiry systemd kills
        # the walk and ExecStopPost brings the instance back.
        f"--property=RuntimeMaxSec={_RUNTIME_MAX_SECONDS}",
        # reset-failed first: an exhausted start-rate-limit budget refuses every
        # start, including this failsafe, which is when it matters most.
        f'--property=ExecStopPost=/bin/sh -c "{systemctl} reset-failed {OPENHOST_UNIT}; '
        f'{systemctl} start --no-block {OPENHOST_UNIT}"',
        f"--setenv={DETACHED_ENV}=1",
        # Transient units get no HOME, and git needs one (root's safe.directory
        # lives in $HOME/.gitconfig), or the walk dies on its first git call.
        f"--setenv=HOME={os.environ.get('HOME') or '/root'}",
    ]
    data_dir = os.environ.get(DATA_DIR_ENV)
    if data_dir:
        cmd.append(f"--setenv={DATA_DIR_ENV}={data_dir}")
    cmd += [sys.executable, "-c", _ENTRYPOINT]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"failed to launch {APPLY_UNIT}: {e}") from e

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # The unit name is the real mutex: compute_space's in-process lock dies
        # with the service this walk stops.
        if "already exists" in stderr:
            raise ApplyAlreadyRunningError("An update is already in progress.")
        raise RuntimeError(f"systemd-run for {APPLY_UNIT} exited {result.returncode}: {stderr}")

    logger.info(f"apply detached as {APPLY_UNIT}")


def stop_openhost() -> None:
    """Stop openhost for the walk.

    A unit that is not loaded is nothing to stop: a baseline host has no
    openhost.service until this walk's migrations install it. Anything else raises,
    because carrying on would run migrations against a live router.
    """
    logger.info(f"stopping {OPENHOST_UNIT} for the apply")
    result = _systemctl("stop", OPENHOST_UNIT)
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    if "not loaded" in stderr or "not found" in stderr:
        return
    raise RuntimeError(f"systemctl stop {OPENHOST_UNIT} exited {result.returncode}: {stderr}")


def start_openhost() -> None:
    """Bring openhost back after the walk.

    `restart`, not `start`, so freshly installed code still boots if something
    started the service mid-walk; reset-failed first so an exhausted
    start-rate-limit cannot refuse it.
    """
    logger.info(f"starting {OPENHOST_UNIT} after the apply")
    reset_openhost_start_limit()
    result = _systemctl("restart", OPENHOST_UNIT)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl restart {OPENHOST_UNIT} exited {result.returncode}: {(result.stderr or '').strip()}"
        )


def reset_openhost_start_limit() -> None:
    """Reset systemd's activation-rate counter before an intentional restart."""
    result = _systemctl("reset-failed", OPENHOST_UNIT, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl reset-failed {OPENHOST_UNIT} exited {result.returncode}: {(result.stderr or '').strip()}"
        )
