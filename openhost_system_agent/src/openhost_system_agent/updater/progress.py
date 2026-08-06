from __future__ import annotations

import datetime
import json
import os
import shutil

import attr

from openhost_system_agent.updater.paths import progress_log_path
from openhost_system_agent.updater.paths import updater_dir


def _ensure_updater_dir() -> None:
    """Create the updater dir, keeping it writable by the ``host`` service user.

    The apply walk writes progress here as root, but compute_space (running as
    ``host``) must also write the update token into it. If root created the dir,
    ``host`` would get EACCES. So when running as root we chown the dir back to
    ``host`` — mirroring reclaim_host_ownership's failsafe — so both sides can
    write it. Best-effort; never raises.
    """
    d = updater_dir()
    d.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        try:
            shutil.chown(d, user="host", group="host")
        except (OSError, LookupError):
            pass


# Terminal phases: once one is written the update is over (the browser stops
# polling and reloads on "done").
PHASE_DONE = "done"
PHASE_FAILED = "failed"


@attr.s(auto_attribs=True, frozen=True)
class ProgressEntry:
    # ``phase`` is a short machine token (fetch/checkout/migrate/install/done/
    # failed); ``message`` is human-readable text shown to the owner; ``ref`` is
    # the release tag/ref being applied, when relevant.
    ts: str
    phase: str
    message: str
    ref: str | None = None


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


# NOTE: writes here are intentionally best-effort. This is cosmetic telemetry the
# updater UI tails; a logging failure must never abort or delay a real host
# update, so these swallow errors rather than failing loudly.
def reset_progress() -> None:
    """Truncate the progress log at the start of a fresh apply so no stale run is shown."""
    try:
        _ensure_updater_dir()
        path = progress_log_path()
        path.write_text("")
        path.chmod(0o644)
    except OSError:
        pass


def record(phase: str, message: str, ref: str | None = None) -> None:
    """Append one progress entry as a JSON line. Best-effort; never raises."""
    entry = ProgressEntry(ts=_now(), phase=phase, message=message, ref=ref)
    try:
        _ensure_updater_dir()
        with open(progress_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(attr.asdict(entry)) + "\n")
    except OSError:
        pass


def read_entries() -> list[dict[str, object]]:
    """Read the progress log, tolerating a partially-written final line.

    Shared reader used by both the detached updater and compute_space so the two
    can't drift on the JSONL parsing. Best-effort: a missing/unreadable log yields
    an empty list. Never raises.
    """
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
            # A half-written final line; skip until it is complete.
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def is_terminal(entries: list[dict[str, object]]) -> bool:
    """True once the last entry is a terminal phase (done/failed) — the update is over."""
    return bool(entries) and entries[-1].get("phase") in (PHASE_DONE, PHASE_FAILED)
