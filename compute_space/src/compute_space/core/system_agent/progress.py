from __future__ import annotations

import attr

import openhost_system_agent.updater.progress as agent_progress

# The agent's reader/writer, so compute_space and the updater parse the same log.
from compute_space.core.logging import logger
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.core.system_agent.client import system_agent_mark_boot_complete_sync
from compute_space.core.system_agent.client import system_agent_record_update_failure


@attr.s(auto_attribs=True, frozen=True)
class ProgressView:
    entries: list[dict[str, object]]
    terminal: bool


def read_progress() -> ProgressView:
    """Read the update progress log, tolerating a partial last line. Never raises."""
    entries = agent_progress.read_entries()
    return ProgressView(entries=entries, terminal=agent_progress.is_terminal(entries))


def mark_boot_complete() -> None:
    """Finalize the log at boot so the /updating page can leave (see the agent's
    mark_boot_complete). Falls back to the root agent for a root-owned log."""
    try:
        if agent_progress.mark_boot_complete():
            return
    except Exception:
        logger.exception("failed to finalize the update progress log directly")
    try:
        system_agent_mark_boot_complete_sync()
    except SystemAgentError:
        logger.exception("failed to finalize the update progress log via the agent")


async def record_apply_failure(message: str) -> None:
    """Ensure the log ends terminal so the /updating page stops polling. Falls back
    to the root agent for a root-owned log."""
    try:
        if agent_progress.record_failure_if_not_terminal(message):
            return
    except Exception:
        logger.exception("failed to record apply failure directly")
    try:
        await system_agent_record_update_failure(message)
    except SystemAgentError:
        logger.exception("failed to record apply failure via the agent")
