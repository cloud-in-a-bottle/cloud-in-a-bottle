from __future__ import annotations

import datetime
import json
import os
import shutil

import attr

from openhost_system_agent.updater.paths import progress_log_path
from openhost_system_agent.updater.paths import updater_dir


def _ensure_updater_dir() -> None:
    # The apply walk writes here as root, but compute_space (as ``host``) must
    # also write the token, so when running as root chown the dir back to
    # ``host`` — otherwise host would get EACCES. Best-effort; never raises.
    d = updater_dir()
    d.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        try:
            shutil.chown(d, user="host", group="host")
        except (OSError, LookupError):
            pass


PHASE_DONE = "done"
PHASE_FAILED = "failed"


@attr.s(auto_attribs=True, frozen=True)
class ProgressEntry:
    ts: str
    phase: str
    message: str
    ref: str | None = None


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


# Writes here are best-effort: this is cosmetic telemetry, so a logging failure
# must never abort or delay a real host update.
def reset_progress() -> None:
    """Truncate the progress log at the start of a fresh apply."""
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
    """Read the progress log, tolerating a partially-written final line. Never raises."""
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
    """True once the last entry is a terminal phase (done/failed)."""
    return bool(entries) and entries[-1].get("phase") in (PHASE_DONE, PHASE_FAILED)
