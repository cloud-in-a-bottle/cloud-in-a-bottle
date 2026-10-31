from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta

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

# Pause after an unexpected error before trying again, so a persistent one doesn't spin.
ERROR_BACKOFF = timedelta(minutes=5)

# How often to check whether a run is due. asyncio's timers stop while the VM is paused, so a long sleep until the
# scheduled time would fire late by however long the pause lasted; a short poll against the wall clock catches up soon
# after the VM resumes.
POLL_INTERVAL = timedelta(minutes=1)

# Scheduled times before this were not missed: a fresh instance waits for its first scheduled time rather than
# updating at boot.
_process_started_at = datetime.now(UTC)
# Set when the owner changes the schedule, so picking an earlier time (or turning updates on) doesn't count today's
# already-passed time as missed and update immediately.
_schedule_changed_at: datetime | None = None


def last_scheduled_at(time_utc: time, now: datetime) -> datetime:
    """The most recent scheduled time at or before ``now``."""
    scheduled = datetime.combine(now.date(), time_utc, tzinfo=UTC)
    if scheduled > now:
        scheduled -= timedelta(days=1)
    return scheduled


def is_due(config: AutoUpdateConfig, since: datetime, now: datetime) -> bool:
    """True if a scheduled time has passed since ``since``, so a run missed while the VM was paused or the process was
    down happens as soon as it can."""
    return config.enabled and last_scheduled_at(config.time_utc, now) > since


def reschedule_auto_update() -> None:
    """Call after the schedule changes."""
    global _schedule_changed_at
    _schedule_changed_at = datetime.now(UTC)


def _record(run: AutoUpdateLastRun) -> None:
    logger.info(f"scheduled update: {run.describe()}")
    with closing(get_db()) as db:
        record_last_run(db, run)


async def _check(now: datetime) -> AutoUpdateLastRun | None:
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
        skipped = await _check(now)
    except SystemAgentError as e:
        _record(
            AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.FAILED, detail=f"could not check for updates: {e}")
        )
        return
    if skipped is not None:
        _record(skipped)
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


async def _run_if_due() -> None:
    with closing(get_db()) as db:
        config = read_auto_update_config(db)
        last_run = read_last_run(db)
    # The last run, not the process start, once there is one: a restart mustn't hide a run missed while it was down.
    since = last_run.at if last_run is not None else _process_started_at
    if _schedule_changed_at is not None:
        since = max(since, _schedule_changed_at)
    if is_due(config, since, datetime.now(UTC)):
        await run_auto_update()


def start_auto_update_task() -> asyncio.Task[None]:
    """Run the update once after each scheduled time, catching up on one missed while the VM was paused or the process
    was down."""

    async def _run() -> None:
        while True:
            # Everything inside the try: an escaped exception would end the task, silently stopping auto-updates
            # until the next restart.
            try:
                await _run_if_due()
            except Exception:
                logger.exception(f"scheduled update failed; retrying in {ERROR_BACKOFF}")
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())
                continue
            await asyncio.sleep(POLL_INTERVAL.total_seconds())

    return asyncio.create_task(_run(), name="auto-update")
