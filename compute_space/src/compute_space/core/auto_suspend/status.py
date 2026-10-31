import sqlite3
from datetime import datetime
from datetime import time
from enum import StrEnum

import attr

from compute_space.core.auto_suspend.activity import last_valid_request_at
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.runner import scheduled_run_in_progress
from compute_space.core.system_agent.client import apply_lock
from compute_space.core.tls.provision import cert_issuance_in_progress


class BusyReason(StrEnum):
    # An openhost update is being checked for or started. Once it is underway this process is stopped, and the downtime updater
    # answers 503 until the updated instance is back.
    PLATFORM_UPDATE = "platform_update"
    TLS_CERTIFICATE = "tls_certificate"


@attr.s(auto_attribs=True, frozen=True)
class AutoSuspendStatus:
    # Work that suspending the instance now would stall. Empty when it can be suspended.
    busy_reasons: tuple[BusyReason, ...]
    # When the owner has openhost update itself each day, or None with auto-updates off. A hint for when to schedule
    # the daily wake: the update runs on the first wake after this time.
    auto_update_time_utc: time | None
    # The last inbound request with valid auth or to an app's public path, or None if there's been none since the
    # router started. Unlike raw traffic, scanners probing for paths that don't exist don't move it.
    last_valid_request_at: datetime | None


def get_auto_suspend_status(db: sqlite3.Connection) -> AutoSuspendStatus:
    reasons: list[BusyReason] = []
    if apply_lock.locked() or scheduled_run_in_progress():
        reasons.append(BusyReason.PLATFORM_UPDATE)
    if cert_issuance_in_progress():
        reasons.append(BusyReason.TLS_CERTIFICATE)
    auto_update = read_auto_update_config(db)
    return AutoSuspendStatus(
        busy_reasons=tuple(reasons),
        auto_update_time_utc=auto_update.time_utc if auto_update.enabled else None,
        last_valid_request_at=last_valid_request_at(),
    )
