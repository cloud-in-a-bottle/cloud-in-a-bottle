from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import StrEnum

import attr

from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

APP_ENABLED_KEY = "app_auto_update_enabled"
APP_LAST_RUN_KEY = "app_auto_update_last_run"


class AppUpdateOutcome(StrEnum):
    UPDATED = "updated"
    UP_TO_DATE = "up_to_date"
    # The update changes settings or declares permissions the owner must approve; the app keeps its current version.
    NEEDS_APPROVAL = "needs_approval"
    SKIPPED = "skipped"
    FAILED = "failed"


@attr.s(auto_attribs=True, frozen=True)
class AppUpdateResult:
    app_id: str
    app_name: str
    outcome: AppUpdateOutcome
    # Why it was skipped, what needs approving, or what went wrong.
    detail: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AppAutoUpdateLastRun:
    at: datetime
    results: list[AppUpdateResult]


def read_app_auto_update_enabled(db: sqlite3.Connection) -> bool:
    # Off until the owner opts in, on every instance: an app update can rebuild and restart the app.
    raw = get_setting(db, APP_ENABLED_KEY)
    if raw not in (None, "0", "1"):
        raise ValueError(f"invalid {APP_ENABLED_KEY} setting: {raw!r}")
    return raw == "1"


def write_app_auto_update_enabled(db: sqlite3.Connection, enabled: bool) -> None:
    set_setting(db, APP_ENABLED_KEY, "1" if enabled else "0")


def read_app_last_run(db: sqlite3.Connection) -> AppAutoUpdateLastRun | None:
    raw = get_setting(db, APP_LAST_RUN_KEY)
    if raw is None:
        return None
    data = json.loads(raw)
    return AppAutoUpdateLastRun(
        at=datetime.fromisoformat(data["at"]),
        results=[
            AppUpdateResult(
                app_id=r["app_id"],
                app_name=r["app_name"],
                outcome=AppUpdateOutcome(r["outcome"]),
                detail=r["detail"],
            )
            for r in data["results"]
        ],
    )


def record_app_last_run(db: sqlite3.Connection, run: AppAutoUpdateLastRun) -> None:
    payload = {"at": run.at.isoformat(), "results": [attr.asdict(r) for r in run.results]}
    set_setting(db, APP_LAST_RUN_KEY, json.dumps(payload))
