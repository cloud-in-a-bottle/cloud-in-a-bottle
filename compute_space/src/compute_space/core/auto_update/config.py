from __future__ import annotations

import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import time

import attr

from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

ENABLED_KEY = "auto_update_enabled"
TIME_UTC_KEY = "auto_update_time_utc"
LAST_ATTEMPT_AT_KEY = "auto_update_last_attempt_at"
LAST_RESULT_KEY = "auto_update_last_result"

# Until the owner picks a time, updates run at this hour in the server's timezone.
DEFAULT_LOCAL_TIME = time(4, 0)


@attr.s(auto_attribs=True, frozen=True)
class AutoUpdateConfig:
    enabled: bool
    # Naive time of day, in UTC.
    time_utc: time


@attr.s(auto_attribs=True, frozen=True)
class AutoUpdateLastRun:
    at: datetime
    result: str


def default_time_utc(now: datetime) -> time:
    local = now.astimezone().replace(
        hour=DEFAULT_LOCAL_TIME.hour, minute=DEFAULT_LOCAL_TIME.minute, second=0, microsecond=0
    )
    return local.astimezone(UTC).time()


def read_auto_update_config(db: sqlite3.Connection, now: datetime) -> AutoUpdateConfig:
    # No row means a fresh instance; migration v15 seeds "0" on instances that predate auto-updates, so they keep not
    # updating themselves until the owner opts in.
    enabled_raw = get_setting(db, ENABLED_KEY)
    if enabled_raw not in (None, "0", "1"):
        raise ValueError(f"invalid {ENABLED_KEY} setting: {enabled_raw!r}")
    time_raw = get_setting(db, TIME_UTC_KEY)
    return AutoUpdateConfig(
        enabled=enabled_raw != "0",
        time_utc=time.fromisoformat(time_raw) if time_raw else default_time_utc(now),
    )


def write_auto_update_config(db: sqlite3.Connection, config: AutoUpdateConfig) -> None:
    set_setting(db, ENABLED_KEY, "1" if config.enabled else "0")
    set_setting(db, TIME_UTC_KEY, config.time_utc.strftime("%H:%M"))


def read_last_run(db: sqlite3.Connection) -> AutoUpdateLastRun | None:
    at_raw = get_setting(db, LAST_ATTEMPT_AT_KEY)
    if at_raw is None:
        return None
    return AutoUpdateLastRun(at=datetime.fromisoformat(at_raw), result=get_setting(db, LAST_RESULT_KEY) or "")


def record_last_run(db: sqlite3.Connection, run: AutoUpdateLastRun) -> None:
    set_setting(db, LAST_ATTEMPT_AT_KEY, run.at.isoformat())
    set_setting(db, LAST_RESULT_KEY, run.result)
