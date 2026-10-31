import sqlite3
import subprocess
from datetime import time
from enum import StrEnum

import attr

from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.runner import scheduled_run_in_progress
from compute_space.core.operation_locks import operation_lock
from compute_space.core.system_agent.client import apply_lock
from compute_space.core.tls.provision import cert_issuance_in_progress
from compute_space.core.util import async_wrap

# Ubuntu's unattended upgrades: apt-daily refreshes the package lists and downloads updates, apt-daily-upgrade installs
# them. Both are oneshot units, so they're "activating" for as long as they run.
_APT_UNITS = ("apt-daily.service", "apt-daily-upgrade.service")
_SYSTEMD_ACTIVE_STATES = frozenset({"active", "activating", "deactivating", "reloading"})
_SYSTEMD_IDLE_STATES = frozenset({"inactive", "failed"})


class BusyReason(StrEnum):
    # An openhost update is being checked for or started. Once it is underway this process is stopped, and the downtime updater
    # answers 503 until the updated instance is back.
    PLATFORM_UPDATE = "platform_update"
    OS_PACKAGES = "os_packages"
    TLS_CERTIFICATE = "tls_certificate"
    # An app is building, starting or being removed.
    APPS = "apps"
    # A domain or archive backend change, which can restart the router.
    PLATFORM_OPERATION = "platform_operation"


@attr.s(auto_attribs=True, frozen=True)
class AutoSuspendStatus:
    # Work that suspending the instance now would stall. Empty when it can be suspended.
    busy_reasons: tuple[BusyReason, ...]
    # When the owner has openhost update itself each day, or None with auto-updates off. A hint for when to schedule
    # the daily wake: the update runs on the first wake after this time.
    auto_update_time_utc: time | None


@async_wrap
def _apt_running() -> bool:
    result = subprocess.run(["systemctl", "is-active", *_APT_UNITS], capture_output=True, text=True, timeout=15)
    states = result.stdout.split()
    # is-active exits non-zero when no unit is active, so judge by the states it prints instead.
    if len(states) != len(_APT_UNITS) or not set(states) <= _SYSTEMD_ACTIVE_STATES | _SYSTEMD_IDLE_STATES:
        raise RuntimeError(f"could not determine the state of {_APT_UNITS}: {result.stderr or result.stdout}")
    return any(state in _SYSTEMD_ACTIVE_STATES for state in states)


def _apps_busy(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT 1 FROM apps WHERE status IN ('building', 'starting', 'removing') LIMIT 1").fetchone()
    return row is not None


async def get_auto_suspend_status(db: sqlite3.Connection) -> AutoSuspendStatus:
    reasons: list[BusyReason] = []
    if apply_lock.locked() or scheduled_run_in_progress():
        reasons.append(BusyReason.PLATFORM_UPDATE)
    if await _apt_running():
        reasons.append(BusyReason.OS_PACKAGES)
    if cert_issuance_in_progress():
        reasons.append(BusyReason.TLS_CERTIFICATE)
    if _apps_busy(db):
        reasons.append(BusyReason.APPS)
    if operation_lock.locked():
        reasons.append(BusyReason.PLATFORM_OPERATION)
    auto_update = read_auto_update_config(db)
    return AutoSuspendStatus(
        busy_reasons=tuple(reasons),
        auto_update_time_utc=auto_update.time_utc if auto_update.enabled else None,
    )
