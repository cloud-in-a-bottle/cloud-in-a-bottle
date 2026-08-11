from __future__ import annotations

import attr

# Reuse the agent's shared reader/writer so compute_space and the detached
# updater parse and produce the same JSONL log identically. It resolves the log
# path from OPENHOST_DATA_DIR (set by web/start.py).
from compute_space.core.logging import logger
from compute_space.core.system_agent import SystemAgentError
from compute_space.core.system_agent import system_agent_mark_boot_complete_sync
from compute_space.core.system_agent import system_agent_record_update_failure
from openhost_system_agent.updater import progress as agent_progress


@attr.s(auto_attribs=True, frozen=True)
class ProgressView:
    entries: list[dict[str, object]]
    terminal: bool


def read_progress() -> ProgressView:
    """Read the update progress log, tolerating a partial last line. Never raises."""
    entries = agent_progress.read_entries()
    return ProgressView(entries=entries, terminal=agent_progress.is_terminal(entries))


def mark_boot_complete() -> None:
    """Append the terminal "done" entry if the previous run ended in a restart.

    The apply walk records a non-terminal "restarting" right before
    `systemctl restart openhost`; only the NEW process appends "done", so the
    /updating page can't be bounced back to a dashboard that is about to die
    (the old instance answering /health 200 after a premature terminal entry).

    Writes directly when possible; a log created by an older (pre-host-ownership)
    build is root-owned, so fall back to the root agent for that one transition.
    Called once at boot. Best-effort: the log is cosmetic telemetry.
    """
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
    """Ensure the progress log ends in a terminal "failed" entry.

    The agent records its own failures, but if it dies before doing so (or the
    failure happens on the compute_space side) the /updating page would poll a
    non-terminal log forever. Skips writing when the log is already terminal so
    the agent's more specific message wins. Falls back to the root agent when the
    log is not writable directly (root-owned legacy log). Best-effort.
    """
    try:
        if agent_progress.record_failure_if_not_terminal(message):
            return
    except Exception:
        logger.exception("failed to record apply failure directly")
    try:
        await system_agent_record_update_failure(message)
    except SystemAgentError:
        logger.exception("failed to record apply failure via the agent")
