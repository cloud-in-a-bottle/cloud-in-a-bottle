from __future__ import annotations

import attr

# Reuse the agent's shared progress reader so compute_space and the detached
# updater parse the same JSONL log the exact same way (no drift). It resolves
# the log path from OPENHOST_DATA_DIR, which compute_space sets to its own data
# dir at startup (see web/start.py).
from openhost_system_agent.updater import progress as agent_progress


@attr.s(auto_attribs=True, frozen=True)
class ProgressView:
    # entries: the progress lines so far (each a dict as written by the agent).
    # terminal: True once the last entry is a terminal phase (done/failed), which
    # tells the UI the update is over and it can move on.
    entries: list[dict[str, object]]
    terminal: bool


def read_progress() -> ProgressView:
    """Read the update progress log, tolerating a partially-written last line.

    Best-effort: a missing/unreadable log yields an empty view (the UI just keeps
    polling). Never raises.
    """
    entries = agent_progress.read_entries()
    return ProgressView(entries=entries, terminal=agent_progress.is_terminal(entries))
