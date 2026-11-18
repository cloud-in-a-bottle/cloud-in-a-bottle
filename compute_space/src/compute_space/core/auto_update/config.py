from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import time
from enum import StrEnum
from functools import cache
from zoneinfo import ZoneInfo
from zoneinfo import available_timezones

import attr

from compute_space.core.settings_store import delete_setting
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

ENABLED_KEY = "auto_update_enabled"
TIME_KEY = "auto_update_time"
TIMEZONE_KEY = "auto_update_timezone"
LAST_RUN_AT_KEY = "auto_update_last_run_at"
LAST_RUN_OUTCOME_KEY = "auto_update_last_run_outcome"
LAST_RUN_DETAIL_KEY = "auto_update_last_run_detail"

# Until the owner picks a time, updates run at 04:00 UTC.
DEFAULT_TIME = time(4, 0)
DEFAULT_TIMEZONE = "UTC"


@attr.s(auto_attribs=True, frozen=True)
class AutoUpdateConfig:
    enabled: bool
    # The wall-clock time in `timezone` (an IANA name), so the update keeps the same local time across daylight saving
    # time changes.
    time: time
    timezone: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


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


@cache
def _timezones() -> frozenset[str]:
    # Reads the system's timezone database, so only once.
    return frozenset(available_timezones())


def is_valid_timezone(name: str) -> bool:
    return name in _timezones()


def read_auto_update_config(db: sqlite3.Connection) -> AutoUpdateConfig:
    # No row means a fresh instance; migration v16 seeds "0" on instances that predate auto-updates, so they keep not
    # updating themselves until the owner opts in.
    enabled_raw = get_setting(db, ENABLED_KEY)
    if enabled_raw not in (None, "0", "1"):
        raise ValueError(f"invalid {ENABLED_KEY} setting: {enabled_raw!r}")
    time_raw = get_setting(db, TIME_KEY)
    timezone = get_setting(db, TIMEZONE_KEY) or DEFAULT_TIMEZONE
    if not is_valid_timezone(timezone):
        raise ValueError(f"invalid {TIMEZONE_KEY} setting: {timezone!r}")
    return AutoUpdateConfig(
        enabled=enabled_raw != "0",
        time=time.fromisoformat(time_raw) if time_raw else DEFAULT_TIME,
        timezone=timezone,
    )


def write_auto_update_config(db: sqlite3.Connection, config: AutoUpdateConfig) -> None:
    set_setting(db, ENABLED_KEY, "1" if config.enabled else "0")
    set_setting(db, TIME_KEY, config.time.strftime("%H:%M"))
    set_setting(db, TIMEZONE_KEY, config.timezone)


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
