from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from compute_space.core.auto_update.config import AutoUpdateConfig
from compute_space.core.auto_update.config import AutoUpdateLastRun
from compute_space.core.auto_update.config import AutoUpdateOutcome
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.config import read_last_run
from compute_space.core.auto_update.config import record_last_run
from compute_space.core.logging import logger
from compute_space.core.system_agent.client import ApplyLaunch
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.core.system_agent.client import apply_lock
from compute_space.core.system_agent.client import system_agent_apply
from compute_space.core.system_agent.client import system_agent_fetch
from compute_space.core.system_agent.client import system_agent_get_remote
from compute_space.core.system_agent.client import system_agent_status
from compute_space.db import get_db
from openhost_system_agent.protocol import UpdateChannel

POLL_INTERVAL = timedelta(minutes=1)

_update_due: datetime | None = None
# True from the start of a scheduled run, including the check for a new release before apply_lock is taken.
_scheduled_run_in_progress = False


def scheduled_run_in_progress() -> bool:
    return _scheduled_run_in_progress


def _record(run: AutoUpdateLastRun) -> None:
    logger.info(f"scheduled update: {run.describe()}")
    with closing(get_db()) as db:
        record_last_run(db, run)


async def _check_if_update_possible(now: datetime) -> AutoUpdateLastRun | None:
    """The outcome that stops this scheduled run from updating, or None to go ahead."""
    remote = await system_agent_get_remote()
    if remote.channel != UpdateChannel.TAGS:
        return AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.NOT_ON_TAGS)
    fetch = await system_agent_fetch()
    if fetch.state == "DIRTY":
        return AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.DIRTY)
    status = await system_agent_status()
    if not status.ok and status.reason != "behind":
        return AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.FAILED, detail=status.message)
    # A "behind" host has system migrations pending, which the update applies.
    if fetch.state == "UP_TO_DATE" and status.ok:
        return AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.UP_TO_DATE)
    return None


async def run_auto_update() -> None:
    now = datetime.now(UTC)
    try:
        invalid_outcome = await _check_if_update_possible(now)
    except SystemAgentError as e:
        _record(
            AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.FAILED, detail=f"could not check for updates: {e}")
        )
        return
    if invalid_outcome is not None:
        _record(invalid_outcome)
        return

    if apply_lock.locked():
        _record(AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.ALREADY_RUNNING))
        return
    # The apply stops this process, so there is no later point to record how the update went. Record that it started;
    # the update progress log has the rest. Done before taking the lock so a failed write can't leave it held.
    _record(AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.STARTED))
    # Held for the rest of this process's life once the walk is launched: the apply stops openhost moments later, so
    # releasing early would only let a Settings click race the walk. system_agent_apply releases it if no walk starts.
    await apply_lock.acquire()
    launch = await system_agent_apply()
    if launch == ApplyLaunch.ALREADY_RUNNING:
        _record(AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.ALREADY_RUNNING))
    elif launch == ApplyLaunch.FAILED:
        _record(AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.FAILED, detail="the update could not be started."))


def _at_on(target: time, zone: ZoneInfo, day: date) -> datetime:
    """The UTC instant at which the clock in `zone` reads `target` on `day`. A time skipped when the clocks go forward
    is read with the offset from before the change, so it lands just after the change; a repeated one is the first."""
    return datetime.combine(day, target, tzinfo=zone).astimezone(UTC)


def _next_datetime_at(target: time, zone: ZoneInfo, now: datetime) -> datetime:
    """Return the next instant after `now` at which the clock in `zone` reads `target`."""
    today = now.astimezone(zone).date()
    candidate = _at_on(target, zone, today)
    return candidate if candidate > now else _at_on(target, zone, today + timedelta(days=1))


def _most_recent_datetime_at(target: time, zone: ZoneInfo, now: datetime) -> datetime:
    """Return the latest instant at or before `now` at which the clock in `zone` reads `target`."""
    today = now.astimezone(zone).date()
    candidate = _at_on(target, zone, today)
    return candidate if candidate <= now else _at_on(target, zone, today - timedelta(days=1))


def _first_update_due(target: time, zone: ZoneInfo, last_run_at: datetime | None, now: datetime) -> datetime:
    """When the update is first due after the router starts. The most recent scheduled time is still due if no run has
    happened since, so a restart across it (a cold boot, say) doesn't skip that day. With no run on record, as on a fresh
    install, it waits for the next scheduled time rather than updating on first boot."""
    most_recent = _most_recent_datetime_at(target, zone, now)
    if last_run_at is not None and last_run_at < most_recent:
        return most_recent
    return _next_datetime_at(target, zone, now)


def next_auto_update_at(config: AutoUpdateConfig) -> datetime:
    """The next scheduled time, in UTC."""
    return _next_datetime_at(config.time, config.zone, datetime.now(UTC))


def reschedule_auto_update(config: AutoUpdateConfig) -> None:
    """Call after the schedule changes, so a newly chosen time that already passed today waits for tomorrow rather
    than counting as missed."""
    global _update_due
    _update_due = next_auto_update_at(config)


async def _run_if_due() -> None:
    global _update_due, _scheduled_run_in_progress
    now = datetime.now(UTC)
    with closing(get_db()) as db:
        config = read_auto_update_config(db)
        if _update_due is None:
            last_run = read_last_run(db)
            _update_due = _first_update_due(config.time, config.zone, last_run.at if last_run else None, now)

    if config.enabled and now > _update_due:
        _scheduled_run_in_progress = True
        try:
            await run_auto_update()
        finally:
            _scheduled_run_in_progress = False
            # Even if the run raised, so a failure waits for the next scheduled time. From the time the run ended, not
            # started: a catch-up that runs across today's scheduled time already covers it.
            _update_due = _next_datetime_at(config.time, config.zone, datetime.now(UTC))


def start_auto_update_task() -> asyncio.Task[None]:
    """Run the update automatically on a schedule.

    The intended logic is:
    - Updates are scheduled to run daily at a specific time of day, set by the user.
    - If that window is missed (VM paused for example), the update runs the next time it is up, including after a router restart.
    - (for autosuspending VMs, we'll wake the instance daily sometime around (after) the requested time to allow the update to run)
    - If the update fails, it does not retry until the next scheduled time.
    - If the update time is rescheduled, the next run is scheduled for when the new time is next reached.
    """

    async def _run() -> None:
        while True:
            # Everything inside the try: an escaped exception would end the task, silently stopping auto-updates.
            try:
                await _run_if_due()
            except Exception:
                logger.exception("scheduled update failed; retrying at next window")
            await asyncio.sleep(POLL_INTERVAL.total_seconds())

    return asyncio.create_task(_run(), name="auto-update")
