from __future__ import annotations

import datetime
import json
import os
import shutil

import attr

from openhost_system_agent.updater.paths import progress_log_path
from openhost_system_agent.updater.paths import updater_dir


def _ensure_updater_dir() -> None:
    # Chown back to host when run as root so compute_space (which runs as host)
    # can also write into this dir.
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


# Progress writes are best-effort cosmetic telemetry: never fail a real update.
def reset_progress() -> None:
    try:
        _ensure_updater_dir()
        path = progress_log_path()
        path.write_text("")
        path.chmod(0o644)
    except OSError:
        pass


def record(phase: str, message: str, ref: str | None = None) -> None:
    entry = ProgressEntry(ts=_now(), phase=phase, message=message, ref=ref)
    try:
        _ensure_updater_dir()
        with open(progress_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(attr.asdict(entry)) + "\n")
    except OSError:
        pass


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
