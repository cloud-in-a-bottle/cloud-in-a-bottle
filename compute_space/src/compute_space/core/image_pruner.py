"""Periodic image pruner.

Every app rebuild re-tags ``openhost-{app}:latest`` and orphans the previous
image, so untagged (dangling) layers accumulate over time.  Nothing prunes
them automatically today: the ``/api/drop-docker-cache`` endpoint exists but
runs only on explicit operator action, so on long-lived hosts these dangling
images pile up and can fill the disk.

This module runs a lightweight daemon thread that periodically:

1. Prunes *only* dangling images (``podman image prune`` without ``--all``),
   independent of disk pressure.  Tagged images for stopped apps are kept, so
   nothing has to be rebuilt on next start — which is what makes an
   unconditional periodic prune safe.
2. Sweeps *orphaned tagged* app images: ``openhost-{name}:latest`` images whose
   app no longer exists in the DB (in any status) and which are older than
   ``image_orphan_max_age_seconds``.  App removal already deletes the app's
   image, so this only reclaims tagged images left by a removal that failed or
   predated that logic — the case a dangling-only prune can never catch
   (raised in review).  The age guard prevents reaping an image built for an
   app whose DB row is not yet committed (mid-deploy).

The interval is configured by ``image_prune_interval_seconds`` (0 disables the
thread); orphan pruning by ``image_orphan_max_age_seconds`` (0 disables it).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from compute_space.config import Config
from compute_space.core.containers import list_openhost_images
from compute_space.core.containers import prune_dangling_images
from compute_space.core.containers import remove_image_by_id
from compute_space.core.logging import logger

# Guard so the thread is started at most once per config (mirrors the storage
# guard).  Keyed by db_path because that is the stable per-instance identifier
# already used by the storage guard, and tests can clear it between runs.
_pruner_lock = threading.Lock()
_pruner_db_paths: set[str] = set()


def prune_interval_seconds(config: Config) -> int | None:
    """Return the configured prune interval in seconds, or None if disabled."""
    interval = int(config.image_prune_interval_seconds)
    if interval <= 0:
        return None
    return interval


def orphan_max_age_seconds(config: Config) -> int | None:
    """Return the orphaned-image max age in seconds, or None if disabled."""
    max_age = int(config.image_orphan_max_age_seconds)
    if max_age <= 0:
        return None
    return max_age


def _current_app_names(config: Config) -> set[str]:
    """Return every app name currently in the DB, in any status.

    Includes apps in every status (running, stopped, error, etc.) so an image
    for a merely-stopped or errored app is never treated as orphaned.
    """
    db = sqlite3.connect(config.db_path)
    try:
        rows = db.execute("SELECT name FROM apps").fetchall()
    finally:
        db.close()
    return {row[0] for row in rows if row[0] is not None}


def sweep_orphaned_images(config: Config, now_epoch: float) -> list[str]:
    """Remove orphaned tagged app images older than the configured threshold.

    An image ``openhost-{name}:latest`` is orphaned when ``{name}`` is not the
    name of any current app in the DB.  Only images older than
    ``image_orphan_max_age_seconds`` are removed, so an image whose app row is
    still being committed (mid-deploy) is left alone.  Returns the list of
    removed image ids.  Never raises — failures are logged so the loop survives.
    """
    max_age = orphan_max_age_seconds(config)
    if max_age is None:
        return []

    try:
        app_names = _current_app_names(config)
    except Exception:
        logger.exception("Orphaned-image sweep: could not read apps DB; skipping")
        return []

    removed: list[str] = []
    # Query the DB once; images list once.  Both are small.
    for image in list_openhost_images():
        if image.app_name in app_names:
            continue  # image belongs to a live app (any status) — keep.
        age = now_epoch - image.created_epoch
        if age < max_age:
            # Too new to be sure it's an orphan (e.g. a deploy mid-flight whose
            # DB row hasn't landed yet).  Leave it for a future sweep.
            continue
        logger.info(
            "Removing orphaned image {} (app '{}' no longer exists, age {:.0f}s)",
            image.image_id,
            image.app_name,
            age,
        )
        if remove_image_by_id(image.image_id):
            removed.append(image.image_id)
    return removed


def _run_prune_once(config: Config) -> None:
    """Run one prune cycle (dangling prune + orphan sweep), never raising."""
    try:
        output = prune_dangling_images()
        if output:
            logger.info("Periodic image prune: {}", output)
    except Exception:
        logger.exception("Periodic image prune failed")

    try:
        removed = sweep_orphaned_images(config, time.time())
        if removed:
            logger.info("Orphaned-image sweep removed {} image(s)", len(removed))
    except Exception:
        logger.exception("Orphaned-image sweep failed")


def _image_pruner_loop(config: Config, interval: int) -> None:
    # Sleep first so we don't prune during the startup rush (when a deploy may
    # be mid-build and its intermediate layers are legitimately "dangling").
    while True:
        time.sleep(interval)
        _run_prune_once(config)


def start_image_pruner(config: Config) -> None:
    """Start the periodic image-pruner daemon thread (once per db_path).

    Only starts if ``image_prune_interval_seconds`` is configured (> 0).
    """
    interval = prune_interval_seconds(config)
    if interval is None:
        return
    db_key = os.path.abspath(config.db_path)
    with _pruner_lock:
        if db_key in _pruner_db_paths:
            return
        _pruner_db_paths.add(db_key)
    logger.info("Starting periodic image pruner (every {}s)", interval)
    threading.Thread(target=_image_pruner_loop, args=(config, interval), daemon=True).start()
