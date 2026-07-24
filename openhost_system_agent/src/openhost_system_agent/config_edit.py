"""Agent-side edits to the router ``config.toml`` — the one privileged writer for the config file.

The compute_space router delegates config-file mutation here (via ``sudo openhost_system_agent
config …``) rather than writing the file itself, so all config-file writes go through the agent.
The agent runs as root, so edits preserve the file's existing owner/mode (``host:host``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import attr

# The router config the compute_space service reads (see the openhost.service ``Environment=`` line
# rendered by ansible / v0002_baseline).  Hardcoded so a root command never edits an arbitrary path.
CONFIG_TOML_PATH = "/home/host/.openhost/local_compute_space/config.toml"

# A top-level ``zone_domain = "..."`` assignment line (whole line, including its newline).
_ZONE_DOMAIN_LINE_RE = re.compile(r"(?m)^[ \t]*zone_domain[ \t]*=.*(?:\r?\n|$)")


@attr.s(auto_attribs=True, frozen=True)
class ScrubResult:
    ok: bool
    scrubbed: bool  # True if a line was removed, False if there was nothing to do


def scrub_zone_domain(path: str = CONFIG_TOML_PATH) -> ScrubResult:
    """Remove the legacy ``zone_domain = …`` line from ``config.toml`` (it has been captured into
    the DB and is never read again).  Surgical (only that line), atomic (temp + rename), and
    ownership/mode-preserving.  Idempotent: a no-op if the file is absent or the line isn't present."""
    p = Path(path)
    try:
        original = p.read_text()
    except FileNotFoundError:
        return ScrubResult(ok=True, scrubbed=False)
    scrubbed = _ZONE_DOMAIN_LINE_RE.sub("", original)
    if scrubbed == original:
        return ScrubResult(ok=True, scrubbed=False)

    st = p.stat()  # preserve the existing owner (host:host) + mode across the rewrite
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(scrubbed)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o777)
        os.replace(tmp, p)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return ScrubResult(ok=True, scrubbed=True)
