from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from compute_space.core.auto_update.config import AutoUpdateLastRun
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.config import read_last_run
from compute_space.core.auto_update.config import record_last_run
from compute_space.core.auto_update.schedule import due_slot
from compute_space.core.logging import logger
from compute_space.core.system_agent.apply import ApplyBlockedError
from compute_space.core.system_agent.apply import apply_lock
from compute_space.core.system_agent.apply import check_can_apply
from compute_space.core.system_agent.apply import launch_apply
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.core.system_agent.client import system_agent_fetch
from compute_space.core.system_agent.client import system_agent_get_remote
from compute_space.core.system_agent.client import system_agent_status
from compute_space.db import get_db
from openhost_system_agent.protocol import UpdateChannel

CHECK_INTERVAL = timedelta(minutes=1)


def _record(at: datetime, result: str) -> None:
    with closing(get_db()) as db:
        record_last_run(db, AutoUpdateLastRun(at=at, result=result))


async def _skip_reason() -> str | None:
    """Why this scheduled run should not update, or None to go ahead."""
    remote = await system_agent_get_remote()
    if remote.channel != UpdateChannel.TAGS:
        return "Skipped: automatic updates only run when following tagged releases."
    fetch = await system_agent_fetch()
    if fetch.state == "DIRTY":
        return "Skipped: this instance has uncommitted local changes."
    status = await system_agent_status()
    if fetch.state == "UP_TO_DATE" and status.ok:
        return "Already up to date."
    return None


async def run_auto_update_if_due(now: datetime) -> None:
    with closing(get_db()) as db:
        config = read_auto_update_config(db, now)
        last_run = read_last_run(db)
    if not config.enabled or due_slot(now, config.time_utc, last_run.at if last_run else None) is None:
        return

    # Recorded before doing anything, so a crash or the update's own restart can't retry this slot.
    _record(now, "Checking for updates…")
    try:
        reason = await _skip_reason()
    except SystemAgentError as e:
        _record(now, f"Failed to check for updates: {e}")
        return
    if reason is not None:
        _record(now, reason)
        return

    if apply_lock.locked():
        _record(now, "Skipped: an update is already in progress.")
        return
    await apply_lock.acquire()
    try:
        await check_can_apply()
    except (ApplyBlockedError, SystemAgentError) as e:
        apply_lock.release()
        _record(now, f"Skipped: {e}")
        return

    logger.info("starting scheduled update")
    _record(now, "Started an update to the latest release.")
    if not await launch_apply():
        _record(now, "Failed: the update could not be started.")


def start_auto_update_task() -> asyncio.Task[None]:
    """Check once a minute whether a scheduled update is due. The caller must keep the returned task alive."""

    async def _run() -> None:
        while True:
            try:
                await run_auto_update_if_due(datetime.now(UTC))
            except Exception:
                logger.exception("scheduled update check failed")
            await asyncio.sleep(CHECK_INTERVAL.total_seconds())

    return asyncio.create_task(_run(), name="auto-update")
