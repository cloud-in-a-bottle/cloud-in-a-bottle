from __future__ import annotations

import secrets
import subprocess

from compute_space.core.logging import logger
from compute_space.core.system_agent import SystemAgentError
from compute_space.core.system_agent import system_agent_clear_update_token
from compute_space.core.system_agent import system_agent_set_update_token

# The transient unit the agent runs the apply walk in. Its liveness is the mutex
# that outlives this process, since the walk stops us partway through.
_APPLY_UNIT = "openhost-apply.service"


def apply_is_running() -> bool:
    """True when an apply walk is already in flight on this host.

    Read directly rather than through the root agent: `systemctl is-active` needs
    no privileges, and this sits on the request path. It matters because the
    in-process lock only covers this process's lifetime -- after the walk stops
    and restarts us, the lock is gone but the walk may still be running, and a
    second attempt would overwrite the token the first owner's tab is using.
    """
    # `show` rather than `is-active`: once the transient unit has been collected,
    # asking for it by name makes PID 1 log a failed open on every call, and this
    # is on the request path.
    try:
        result = subprocess.run(
            ["systemctl", "show", "--property=ActiveState", "--value", _APPLY_UNIT],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # Can't tell; the agent's own name-collision check is still the backstop.
        return False
    return result.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def new_update_token() -> str:
    """Mint an unguessable token proving a request to the updater came from the owner tab."""
    return secrets.token_urlsafe(32)


async def persist_update_token(token: str) -> None:
    """Persist the token for the updater, via the root agent. Best-effort."""
    try:
        await system_agent_set_update_token(token)
    except SystemAgentError:
        logger.exception("failed to persist update token; owner will see the generic updating page")


async def clear_update_token() -> None:
    """Remove the update token (called if an update aborts before restart). Best-effort."""
    try:
        await system_agent_clear_update_token()
    except SystemAgentError:
        logger.exception("failed to clear update token")
