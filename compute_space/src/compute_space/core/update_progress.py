from __future__ import annotations

import attr

# Reuse the agent's shared reader so compute_space and the detached updater parse
# the same JSONL log identically. It resolves the log path from OPENHOST_DATA_DIR
# (set by web/start.py).
from openhost_system_agent.updater import progress as agent_progress


@attr.s(auto_attribs=True, frozen=True)
class ProgressView:
    entries: list[dict[str, object]]
    terminal: bool


def read_progress() -> ProgressView:
    """Read the update progress log, tolerating a partial last line. Never raises."""
    entries = agent_progress.read_entries()
    return ProgressView(entries=entries, terminal=agent_progress.is_terminal(entries))
