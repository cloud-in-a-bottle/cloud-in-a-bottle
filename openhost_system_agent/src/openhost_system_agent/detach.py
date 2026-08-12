# Hands the apply walk to systemd as its own transient unit.
#
# compute_space starts the apply, so the apply lives inside openhost.service's
# cgroup (the unit sets no KillMode, so systemd's control-group default applies).
# That makes it impossible for the apply itself to stop openhost: the stop's
# SIGTERM would kill the walk mid-flight, leaving migrations half-applied and
# nobody left to start the service again. So `update apply` re-launches itself
# out of that cgroup before touching anything.
#
# ExecStopPost is the failsafe. However the walk ends -- cleanly, with an
# exception, OOM-killed, SIGKILLed, or killed by RuntimeMaxSec -- systemd still
# resets the start-rate-limit and starts openhost, so no exit path can leave the
# instance stopped. It is a no-op when the walk already started the service.

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

# Set on the transient unit so the re-entered `update apply` knows it is already
# detached and proceeds with the walk instead of handing itself off forever.
DETACHED_ENV = "OPENHOST_APPLY_DETACHED"

# systemd-run reports a name collision when a previous apply is still running.
# That is the concurrency guard: the unit name is the mutex, which matters
# because compute_space's in-process lock dies with the service we stop.
_ALREADY_EXISTS = "already exists"

# A never-provisioned host has no openhost.service to stop; the migrations in
# this walk install it. Mirrors the same tolerance in the updater's launcher.
_NOT_LOADED_PATTERNS = ("not loaded", "not found")

# Ceiling for --wait. Generous: a multi-hop walk runs one pixi install per hop.
_WAIT_TIMEOUT_SECONDS = 3600.0

# Hard ceiling on the walk itself, enforced by systemd. Matches the updater's own
# lifetime cap so the page and the walk expire together rather than leaving one
# serving with nothing behind it.
_RUNTIME_MAX_SECONDS = 3600

# `-c` rather than `-m`: under -m the module loads as __main__ and cappa's
# dispatch exits without running the command (same reason as the updater).
_ENTRYPOINT = (
    "import sys; sys.argv=['openhost_system_agent','update','apply']; "
    "from openhost_system_agent.cli import main; main()"
)


class ApplyAlreadyRunningError(RuntimeError):
    """A previous apply is still running as APPLY_UNIT."""


def is_detached() -> bool:
    return os.environ.get(DETACHED_ENV) == "1"


def systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


def apply_is_running() -> bool:
    """True when an apply unit is already active.

    Checked before the progress log is reset so a second attempt cannot blank the
    log belonging to the update that is actually running.
    """
    # `show` rather than `is-active`: once --collect has reaped the unit, asking
    # for it by name makes PID 1 log "Failed to open /run/systemd/transient/..."
    # on every call, and this runs on the update request path and at every boot.
    try:
        result = subprocess.run(
            ["systemctl", "show", "--property=ActiveState", "--value", APPLY_UNIT],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        # Can't tell; let systemd-run's own name collision be the guard.
        return False
    return result.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def wait_for_apply(timeout: float = _WAIT_TIMEOUT_SECONDS, poll: float = 2.0) -> bool:
    """Block until the apply unit is gone. False if it outlived ``timeout``.

    For callers that want the old synchronous contract back -- scripts and tests
    that need an exit code rather than a progress log. The dashboard never waits:
    the walk stops the router that would be doing the waiting.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not apply_is_running():
            return True
        time.sleep(poll)
    return False


def _systemctl_path() -> str:
    """Absolute path for the unit property; systemd requires one in Exec* lines."""
    return shutil.which("systemctl") or "/usr/bin/systemctl"


def _stop_post_command() -> str:
    """The failsafe that runs however the walk ends.

    `reset-failed` first. openhost.service is start-rate-limited
    (StartLimitBurst=5 / StartLimitIntervalSec=1800), and once that budget is
    exhausted -- by a crash loop burning automatic restarts, or by restarts of an
    already-active unit -- every subsequent start is refused with "Start request
    repeated too quickly", including this one. That was observed on a live box:
    the instance stayed down until someone SSHed in and ran reset-failed, which
    makes the failsafe's promise worthless exactly when it is needed. A stop
    followed by a start does not itself consume the budget, so this is insurance
    rather than routine, and it is a no-op when the unit is not failed.

    `--no-block` so this cannot wait on a job from inside the unit's own
    teardown; the request lives in PID 1 either way.
    """
    systemctl = _systemctl_path()
    return f'/bin/sh -c "{systemctl} reset-failed {OPENHOST_UNIT}; {systemctl} start --no-block {OPENHOST_UNIT}"'


def detach_apply() -> None:
    """Start the walk as APPLY_UNIT. Returns once systemd has accepted the job.

    Raises ApplyAlreadyRunningError if an apply is already in flight, and
    RuntimeError if systemd refuses or systemd-run is missing. There is
    deliberately no inline fallback: without a unit outside openhost's cgroup
    nothing would survive the stop, and stopping a service nothing can start is
    worse than refusing to update.
    """
    if not systemd_run_available():
        raise RuntimeError(
            "systemd-run is not available, so the apply cannot be detached from openhost.service. "
            "Re-running the ansible deploy will reinstall the expected host tooling."
        )
    cmd = [
        "systemd-run",
        f"--unit={APPLY_UNIT}",
        "--description=OpenHost update apply",
        # Collect the unit when it exits so a later apply can reuse the name;
        # the journal and the progress log keep the forensics.
        "--collect",
        # Backstop for a wedged walk. openhost is down for the duration, so an
        # unbounded step (a stalled network call, a hung subprocess) would keep it
        # down indefinitely. On expiry systemd kills the walk and ExecStopPost
        # brings the instance back.
        f"--property=RuntimeMaxSec={_RUNTIME_MAX_SECONDS}",
        f"--property=ExecStopPost={_stop_post_command()}",
        f"--setenv={DETACHED_ENV}=1",
        # Transient units inherit only the manager environment, which has no
        # HOME. git needs one for `git config --global` (and root's
        # safe.directory lives in $HOME/.gitconfig), so without this the walk
        # dies on its first git call with "fatal: $HOME not set".
        f"--setenv=HOME={os.environ.get('HOME') or '/root'}",
    ]
    # compute_space forwards the data-dir override to us; carry it across so the
    # detached walk resolves the same progress log, token and certs.
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
        if _ALREADY_EXISTS in stderr:
            raise ApplyAlreadyRunningError("An update is already in progress.")
        raise RuntimeError(f"systemd-run for {APPLY_UNIT} exited {result.returncode}: {stderr}")

    logger.info(f"apply detached as {APPLY_UNIT}")


def stop_openhost() -> None:
    """Stop openhost for the length of the walk.

    A unit that is not loaded is nothing to stop, so that counts as success: a
    baseline host has no openhost.service until the migrations in this very walk
    install it. Every other failure raises, because carrying on would run
    migrations against a live router -- the thing the stop exists to prevent.
    """
    logger.info(f"stopping {OPENHOST_UNIT} for the apply")
    result = subprocess.run(["systemctl", "stop", OPENHOST_UNIT], capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    if any(pattern in stderr for pattern in _NOT_LOADED_PATTERNS):
        logger.info(f"{OPENHOST_UNIT} is not loaded; nothing to stop")
        return
    raise RuntimeError(f"systemctl stop {OPENHOST_UNIT} exited {result.returncode}: {stderr}")


def start_openhost() -> None:
    """Bring openhost back after the walk. Raises if systemd refuses the job.

    `restart`, not `start`: if anything started the service while the walk was
    running (an operator, or the ExecStopPost of an earlier attempt), a `start`
    would be a silent no-op and the freshly installed code would never boot.

    `reset-failed` first so a previously exhausted start-rate-limit budget cannot
    refuse this restart -- see _stop_post_command. Restarting an already-active
    unit does count against that budget, which is exactly the case `restart`
    exists to handle here.
    """
    logger.info(f"starting {OPENHOST_UNIT} after the apply")
    subprocess.run(["systemctl", "reset-failed", OPENHOST_UNIT], capture_output=True, text=True, timeout=30)
    result = subprocess.run(["systemctl", "restart", OPENHOST_UNIT], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl restart {OPENHOST_UNIT} exited {result.returncode}: {(result.stderr or '').strip()}"
        )
