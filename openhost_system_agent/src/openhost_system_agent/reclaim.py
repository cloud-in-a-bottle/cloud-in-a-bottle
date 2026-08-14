"""Reclaim ownership of the host's Cloud in a Bottle trees.

The Cloud in a Bottle service runs as the unprivileged ``host`` user: it invokes ``git``
and ``pixi run`` against ``/home/host/openhost`` (the repo, including its
``.pixi`` env) and against ``/home/host/.pixi`` (the pixi binary + per-user
caches). All of it must stay ``host``-owned.

The update walk runs as root, though: it applies migrations, runs
``git checkout`` / ``git clean``, and (in older versions) ran ``pixi install``
as root — all of which can leave root-owned files in these trees. The host
service then can't modify them: ``pixi run`` fails with EACCES ("Failed to
update PyPI packages ... Permission denied") and git operations fail on
root-owned objects/index. Either way the service won't start.

This module chowns those trees back to ``host`` so such a mistake self-heals
instead of bricking the host. It is a failsafe: the code paths that install
deps and update the repo already run as ``host`` where possible; this reclaims
any residue left by older buggy versions or by a stray root-run command.
"""

from __future__ import annotations

import os
import subprocess

from openhost_system_agent.pixi import HOST_USER

#: Host-owned trees the root-run update walk writes into. ``/home/host/openhost``
#: is the repo (working tree, ``.git``, and its ``.pixi`` env); ``/home/host/.pixi``
#: holds the pixi binary (targeted by ``pixi self-update``) and per-user caches.
_HOST_PATHS = (
    "/home/host/openhost",
    "/home/host/.pixi",
)


def reclaim_host_ownership() -> None:
    """chown the host's Cloud in a Bottle trees back to the host user. Root-only, idempotent.

    Safe to call repeatedly and cheap when nothing is misowned. Missing paths
    are skipped (a fresh host may not have every tree yet). Raises if not run as
    root, since only root can chown files another user owns.
    """
    if os.geteuid() != 0:
        raise RuntimeError("reclaim_host_ownership must be run as root")

    for path in _HOST_PATHS:
        if not os.path.exists(path):
            continue
        # -R to cover the whole tree; -h so symlinks are chowned in place
        # rather than followed. check=True: a failure here means a tree may
        # still be broken, so surface it rather than silently continuing.
        subprocess.run(
            ["chown", "-Rh", f"{HOST_USER}:{HOST_USER}", path],
            check=True,
            timeout=120,
        )
