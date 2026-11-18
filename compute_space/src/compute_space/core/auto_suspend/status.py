import sqlite3
from datetime import time
from enum import StrEnum

import attr

from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.runner import next_auto_update_at
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
    # The UTC time of day of the next automatic update, or None with auto-updates off. A hint for when to schedule the
    # daily wake: the update runs on the first wake after this time. The schedule is a local time, so this moves by an
    # hour when its timezone's daylight saving time starts or ends.
    auto_update_time_utc: time | None


def get_auto_suspend_status(db: sqlite3.Connection) -> AutoSuspendStatus:
    reasons: list[BusyReason] = []
    if apply_lock.locked() or scheduled_run_in_progress():
        reasons.append(BusyReason.PLATFORM_UPDATE)
    if cert_issuance_in_progress():
        reasons.append(BusyReason.TLS_CERTIFICATE)
    auto_update = read_auto_update_config(db)
    return AutoSuspendStatus(
        busy_reasons=tuple(reasons),
        auto_update_time_utc=next_auto_update_at(auto_update).time() if auto_update.enabled else None,
    )
