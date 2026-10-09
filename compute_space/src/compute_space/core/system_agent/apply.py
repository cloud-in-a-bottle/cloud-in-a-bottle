from __future__ import annotations

import asyncio

import anyio

from compute_space.core.logging import logger
from compute_space.core.system_agent.client import system_agent_apply
from compute_space.core.system_agent.client import system_agent_status
from compute_space.core.system_agent.progress import record_apply_failure
from compute_space.core.system_agent.update_token import clear_update_token
from openhost_system_agent.detach import apply_is_running

# Serializes update launches (the Settings button and the auto-update schedule). Held for the rest of this process's
# life once the walk is launched: the apply stops openhost moments later, so releasing early would only let a second
# launch race the walk. A launch failure releases it for a retry.
apply_lock = asyncio.Lock()

# The agent's refusal when a walk is already running. Its log belongs to that walk, so don't terminate it here.
_ALREADY_RUNNING = "already in progress"


class ApplyBlockedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def check_can_apply() -> None:
    """Raise ApplyBlockedError if an update can't be launched now. The caller must hold ``apply_lock``.

    Lets SystemAgentError through when the migration status can't be read.
    """
    migration_status = await system_agent_status()
    if not migration_status.ok and migration_status.reason != "behind":
        raise ApplyBlockedError("migrations_not_ok", migration_status.message)

    # Check the host too: the walk restarts us, so a fresh process can hold a free lock while an apply is still
    # running.
    if await anyio.to_thread.run_sync(apply_is_running):
        raise ApplyBlockedError("update_in_progress", "An update is already in progress.")


async def launch_apply() -> bool:
    """Hand the update to the detached apply unit, which stops openhost next. The caller must hold ``apply_lock``.

    Returns False, with the failure recorded in the progress log, if the walk could not be started.
    """
    try:
        await system_agent_apply()
    except Exception as e:
        if _ALREADY_RUNNING in str(e):
            logger.warning("apply already in progress; leaving its progress log alone")
            return True
        # Not just SystemAgentError: ANY failure must leave the log terminal, or the /updating page would poll
        # forever with no explanation.
        logger.exception("system agent apply failed")
        await record_apply_failure(f"Update failed: {e}")
        await clear_update_token()
        apply_lock.release()
        return False
    return True
