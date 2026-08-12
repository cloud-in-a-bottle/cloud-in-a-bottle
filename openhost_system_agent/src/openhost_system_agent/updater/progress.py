from __future__ import annotations

import datetime
import json
import os
import shutil

import attr

from openhost_system_agent.detach import apply_is_running
from openhost_system_agent.updater.paths import progress_log_path
from openhost_system_agent.updater.paths import updater_dir


def _chown_to_host(path: str | os.PathLike[str]) -> None:
    # Chown back to host when run as root so compute_space (which runs as host)
    # can also write here: it appends the terminal "done" entry on boot and a
    # "failed" entry when the agent dies before recording one itself.
    if os.geteuid() == 0:
        try:
            shutil.chown(path, user="host", group="host")
        except (OSError, LookupError):
            pass


def _ensure_updater_dir() -> None:
    d = updater_dir()
    d.mkdir(parents=True, exist_ok=True)
    _chown_to_host(d)


PHASE_DONE = "done"
PHASE_FAILED = "failed"
# Recorded just before `systemctl restart openhost`. Deliberately NOT terminal:
# only the freshly booted compute_space appends PHASE_DONE, so the /updating page
# can't finish against the old, about-to-die instance and bounce the owner back
# to a dashboard that dies seconds later.
PHASE_RESTARTING = "restarting"


@attr.s(auto_attribs=True, frozen=True)
class ProgressEntry:
    ts: str
    phase: str
    message: str
    ref: str | None = None


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


# Progress writes are best-effort cosmetic telemetry: never fail a real update.
def reset_progress() -> None:
    try:
        _ensure_updater_dir()
        path = progress_log_path()
        path.write_text("")
        path.chmod(0o644)
        _chown_to_host(path)
    except OSError:
        pass


def record(phase: str, message: str, ref: str | None = None) -> bool:
    """Append an entry. Returns False when the log could not be written (e.g. a
    root-owned log from an older build and we're not root) so callers can route
    through the root agent instead."""
    entry = ProgressEntry(ts=_now(), phase=phase, message=message, ref=ref)
    try:
        _ensure_updater_dir()
        path = progress_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(attr.asdict(entry)) + "\n")
        _chown_to_host(path)
        return True
    except OSError:
        return False


def mark_boot_complete() -> bool:
    """Finalize the log on boot so the /updating page can never hang.

    Two cases:

    * ends with "restarting" -- the walk finished and we are the new instance, so
      append the terminal "done".
    * ends mid-walk ("fetch", "migrate", ...) -- the apply died without recording
      anything: OOM, SIGKILL, a reboot. Nothing else will ever finalize that log,
      and the page only stops polling on a terminal entry, so it would spin
      forever against a healthy instance. Record the interruption instead.

    Returns False only when the append was NEEDED but could not be written.
    Shared by compute_space's boot hook (direct write) and the agent's
    `updater mark-booted` (root fallback for legacy root-owned logs).
    """
    entries = read_entries()
    if not entries or is_terminal(entries):
        return True
    if entries[-1].get("phase") == PHASE_RESTARTING:
        return record(PHASE_DONE, "Instance is back online.")
    if apply_is_running():
        return True  # a walk really is in flight (it started us); leave its log alone
    return record(PHASE_FAILED, "Update was interrupted before it finished. The instance restarted; try again.")


def record_failure_if_not_terminal(message: str) -> bool:
    """Append a terminal "failed" unless the log already ended terminally OR in
    "restarting". The latter means the apply reached the final restart, i.e. it
    succeeded and this process is just being torn down by that restart — not a
    failure. A genuine failure leaves the log at an earlier phase.

    Returns False only when the append was NEEDED but could not be written.
    Shared by compute_space's apply-failure path and the agent's `updater fail`.
    """
    entries = read_entries()
    if is_terminal(entries) or (entries and entries[-1].get("phase") == PHASE_RESTARTING):
        return True
    return record(PHASE_FAILED, message)


def read_entries() -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        text = progress_log_path().read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # skip a half-written final line
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def is_terminal(entries: list[dict[str, object]]) -> bool:
    return bool(entries) and entries[-1].get("phase") in (PHASE_DONE, PHASE_FAILED)
