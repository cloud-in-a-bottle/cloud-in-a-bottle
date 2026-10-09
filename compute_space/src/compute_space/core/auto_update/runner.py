from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta

from compute_space.core.auto_update.config import AutoUpdateLastRun
from compute_space.core.auto_update.config import AutoUpdateOutcome
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.config import record_last_run
from compute_space.core.logging import logger
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.core.system_agent.client import apply_lock
from compute_space.core.system_agent.client import system_agent_apply
from compute_space.core.system_agent.client import system_agent_fetch
from compute_space.core.system_agent.client import system_agent_get_remote
from compute_space.core.system_agent.client import system_agent_status
from compute_space.db import get_db
from openhost_system_agent.protocol import UpdateChannel

CHECK_INTERVAL = timedelta(hours=1)


def is_due(now: datetime, time_utc: time) -> bool:
    """True during the CHECK_INTERVAL after the scheduled time. Checks are CHECK_INTERVAL apart, so exactly one lands
    in it each day."""
    scheduled = datetime.combine(now.date(), time_utc, tzinfo=UTC)
    if scheduled > now:
        scheduled -= timedelta(days=1)
    return now - scheduled < CHECK_INTERVAL


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


async def run_auto_update_if_due(now: datetime) -> None:
    with closing(get_db()) as db:
        config = read_auto_update_config(db, now)
    if not config.enabled or not is_due(now, config.time_utc):
        return

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
    # Held for the rest of this process's life once the walk is launched: the apply stops openhost moments later, so
    # releasing early would only let a Settings click race the walk. A launch failure releases it for a retry.
    await apply_lock.acquire()
    # The apply stops this process, so there is no later point to record how the update went. Record that it started;
    # the update progress log has the rest.
    _record(AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.STARTED))
    if not await system_agent_apply():
        _record(AutoUpdateLastRun(at=now, outcome=AutoUpdateOutcome.FAILED, detail="the update could not be started."))


def start_auto_update_task() -> asyncio.Task[None]:
    """Check every CHECK_INTERVAL whether a scheduled update is due. The caller must keep the returned task alive."""

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        next_check = loop.time()
        while True:
            # Sleep first, so a restart (including the one an update ends with) doesn't check again straight away.
            # Scheduled off the previous tick rather than "now" so the interval doesn't drift and skip a day.
            next_check += CHECK_INTERVAL.total_seconds()
            await asyncio.sleep(next_check - loop.time())
            try:
                await run_auto_update_if_due(datetime.now(UTC))
            except Exception:
                logger.exception("scheduled update check failed")

    return asyncio.create_task(_run(), name="auto-update")
