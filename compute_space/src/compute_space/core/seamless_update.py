from __future__ import annotations

import secrets

from compute_space.core.logging import logger
from compute_space.core.system_agent import SystemAgentError
from compute_space.core.system_agent import system_agent_clear_update_token
from compute_space.core.system_agent import system_agent_set_update_token


def new_update_token() -> str:
    """Mint an unguessable update token for the browser tab that started the update.

    It proves a request to the detached updater came from the owner tab (so the
    updater streams live logs to it, not the generic loading page). It is not a
    general auth credential — it only gates progress display during the downtime.
    """
    return secrets.token_urlsafe(32)


async def persist_update_token(token: str) -> None:
    """Persist the token for the updater, via the (root) agent.

    Routed through the agent so the file lands in the root-managed updater dir
    regardless of directory ownership. Best-effort: if it fails the update still
    proceeds; the owner just sees the generic updating page instead of live logs.
    """
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
