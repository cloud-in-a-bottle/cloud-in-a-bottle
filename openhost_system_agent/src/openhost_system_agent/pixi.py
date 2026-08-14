"""Pinned pixi version and the helper that enforces it on a host."""

from __future__ import annotations

import os
import subprocess

PIXI_VERSION = "0.70.2"
PIXI_BIN = "/home/host/.pixi/bin/pixi"

#: The unprivileged user that owns ``/home/host/.pixi`` and runs the Cloud in a Bottle
#: service. Pixi operations must run as this user so they never leave
#: root-owned files behind (see ``reclaim.py``).
HOST_USER = "host"


def ensure_pixi_version() -> None:
    """Pin the host's pixi to PIXI_VERSION. Idempotent.

    Runs ``self-update`` as the ``host`` user (via sudo) even though the
    migration itself runs as root: pixi's binary and its per-user caches live
    under ``/home/host/.pixi``, and a root-run self-update would leave
    root-owned files there that the host service's ``pixi run`` then can't
    modify (the same failure class this migration exists to avoid).
    """
    cmd = [PIXI_BIN, "self-update", "--version", PIXI_VERSION]
    # Drop to the host user when we're root (the migration case). When already
    # running as host (tests, dev), invoke pixi directly.
    if os.geteuid() == 0:
        cmd = ["sudo", "-u", HOST_USER, "-H", *cmd]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"pixi self-update to {PIXI_VERSION} timed out after 120s") from e
    if result.returncode != 0:
        raise RuntimeError(f"pixi self-update to {PIXI_VERSION} failed (exit {result.returncode}):\n{result.stderr}")
