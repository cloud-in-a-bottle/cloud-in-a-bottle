from __future__ import annotations

import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import time
from enum import StrEnum

import attr

from compute_space.core.settings_store import delete_setting
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

ENABLED_KEY = "auto_update_enabled"
TIME_UTC_KEY = "auto_update_time_utc"
LAST_RUN_AT_KEY = "auto_update_last_run_at"
LAST_RUN_OUTCOME_KEY = "auto_update_last_run_outcome"
LAST_RUN_DETAIL_KEY = "auto_update_last_run_detail"

# Until the owner picks a time, updates run at this hour in the server's timezone.
DEFAULT_LOCAL_TIME = time(4, 0)


@attr.s(auto_attribs=True, frozen=True)
class AutoUpdateConfig:
    enabled: bool
    time_utc: time


class AutoUpdateOutcome(StrEnum):
    STARTED = "started"
    UP_TO_DATE = "up_to_date"
    NOT_ON_TAGS = "not_on_tags"
    DIRTY = "dirty"
    ALREADY_RUNNING = "already_running"
    FAILED = "failed"


_OUTCOME_MESSAGES = {
    AutoUpdateOutcome.STARTED: "Started an update to the latest release.",
    AutoUpdateOutcome.UP_TO_DATE: "Already up to date.",
    AutoUpdateOutcome.NOT_ON_TAGS: "Skipped: automatic updates only run when following tagged releases.",
    AutoUpdateOutcome.DIRTY: "Skipped: this instance has uncommitted local changes.",
    AutoUpdateOutcome.ALREADY_RUNNING: "Skipped: an update is already in progress.",
    AutoUpdateOutcome.FAILED: "Failed.",
}


@attr.s(auto_attribs=True, frozen=True)
class AutoUpdateLastRun:
    at: datetime
    outcome: AutoUpdateOutcome
    # What went wrong, for FAILED.
    detail: str | None = None

    def describe(self) -> str:
        if self.outcome == AutoUpdateOutcome.FAILED and self.detail:
            return f"Failed: {self.detail}"
        return _OUTCOME_MESSAGES[self.outcome]


def default_time_utc() -> time:
    local = (
        datetime.now(UTC)
        .astimezone()
        .replace(hour=DEFAULT_LOCAL_TIME.hour, minute=DEFAULT_LOCAL_TIME.minute, second=0, microsecond=0)
    )
    return local.astimezone(UTC).time()


def read_auto_update_config(db: sqlite3.Connection) -> AutoUpdateConfig:
    # No row means a fresh instance; migration v15 seeds "0" on instances that predate auto-updates, so they keep not
    # updating themselves until the owner opts in.
    enabled_raw = get_setting(db, ENABLED_KEY)
    if enabled_raw not in (None, "0", "1"):
        raise ValueError(f"invalid {ENABLED_KEY} setting: {enabled_raw!r}")
    time_raw = get_setting(db, TIME_UTC_KEY)
    return AutoUpdateConfig(
        enabled=enabled_raw != "0",
        time_utc=time.fromisoformat(time_raw) if time_raw else default_time_utc(),
    )


def write_auto_update_config(db: sqlite3.Connection, config: AutoUpdateConfig) -> None:
    set_setting(db, ENABLED_KEY, "1" if config.enabled else "0")
    set_setting(db, TIME_UTC_KEY, config.time_utc.strftime("%H:%M"))


def read_last_run(db: sqlite3.Connection) -> AutoUpdateLastRun | None:
    at_raw = get_setting(db, LAST_RUN_AT_KEY)
    outcome_raw = get_setting(db, LAST_RUN_OUTCOME_KEY)
    if at_raw is None or outcome_raw is None:
        return None
    return AutoUpdateLastRun(
        at=datetime.fromisoformat(at_raw),
        outcome=AutoUpdateOutcome(outcome_raw),
        detail=get_setting(db, LAST_RUN_DETAIL_KEY),
    )


def record_last_run(db: sqlite3.Connection, run: AutoUpdateLastRun) -> None:
    set_setting(db, LAST_RUN_AT_KEY, run.at.isoformat())
    set_setting(db, LAST_RUN_OUTCOME_KEY, run.outcome)
    if run.detail is None:
        delete_setting(db, LAST_RUN_DETAIL_KEY)
    else:
        set_setting(db, LAST_RUN_DETAIL_KEY, run.detail)
