"""Storage usage helpers and the storage guard.

Reports disk usage totals and per-app breakdowns for the System page.
The storage guard runs as a daemon thread that periodically checks disk free
space and stops apps when free space drops below ``storage_min_free_mb``.  It is
enabled by default with a modest threshold so a runaway disk can't silently take
an instance fully down; operators change the threshold (or disable it by setting
0) in the router config and reboot.  It can be paused from the System page so a
user can start one app (e.g. a file browser) to clean up data before re-enabling
enforcement.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time

from compute_space.config import Config
from compute_space.core.containers import container_image_storage_bytes
from compute_space.core.containers import stop_app_process
from compute_space.core.logging import logger

_MIB = 1024 * 1024

# ---------------------------------------------------------------------------
# Guard state
# ---------------------------------------------------------------------------

_STORAGE_GUARD_INTERVAL_SECONDS = 60

_guard_paused: bool = False
_guard_lock = threading.Lock()
_guard_db_paths: set[str] = set()


def is_guard_paused() -> bool:
    with _guard_lock:
        return _guard_paused


def set_guard_paused(paused: bool) -> None:
    with _guard_lock:
        global _guard_paused
        _guard_paused = paused
    state = "paused" if paused else "resumed"
    logger.info("Storage guard %s", state)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_bytes(size: int) -> str:
    """Format bytes as a human-friendly string."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(size)} B"


# ---------------------------------------------------------------------------
# Usage helpers
# ---------------------------------------------------------------------------


def _ensure_storage_roots(config: Config) -> None:
    os.makedirs(config.persistent_data_dir, exist_ok=True)
    os.makedirs(config.temporary_data_dir, exist_ok=True)
    os.makedirs(os.path.join(config.persistent_data_dir, "vm_data"), exist_ok=True)
    os.makedirs(os.path.join(config.persistent_data_dir, "app_data"), exist_ok=True)


def _dir_size_bytes(path: str) -> int:
    """Return total size of all files under *path* in bytes.

    There is no stdlib equivalent — ``shutil.disk_usage`` reports whole-disk
    stats, not per-directory totals.  Uses ``os.scandir`` recursion rather
    than ``os.walk`` + ``getsize`` so each file is stat'd once, not twice —
    this endpoint walks entire app data trees.  Symlinks are not followed.
    """
    try:
        entries = os.scandir(path)
    except OSError:
        return 0
    total = 0
    with entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total += _dir_size_bytes(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def storage_min_free_bytes(config: Config) -> int | None:
    """Return the configured minimum free space in bytes, or None if not set."""
    limit = int(config.storage_min_free_mb)
    if limit <= 0:
        return None
    return limit * _MIB


def disk_free_bytes(config: Config) -> int:
    """Return free bytes on the data disk."""
    _ensure_storage_roots(config)
    return shutil.disk_usage(config.data_root_dir).free


def openhost_data_usage_bytes(config: Config) -> int:
    _ensure_storage_roots(config)
    return _dir_size_bytes(os.path.join(config.persistent_data_dir, "vm_data"))


def per_app_usage(config: Config) -> dict[str, int]:
    """Return ``{app_name: bytes_used}`` for each subdirectory of app_data."""
    _ensure_storage_roots(config)
    app_data_root = os.path.join(config.persistent_data_dir, "app_data")
    result: dict[str, int] = {}
    if not os.path.isdir(app_data_root):
        return result
    for entry in os.listdir(app_data_root):
        entry_path = os.path.join(app_data_root, entry)
        if os.path.isdir(entry_path):
            result[entry] = _dir_size_bytes(entry_path)
    return result


def _app_data_loose_file_bytes(config: Config) -> int:
    """Size of files sitting directly in app_data (not inside a per-app dir)."""
    app_data_root = os.path.join(config.persistent_data_dir, "app_data")
    try:
        entries = os.scandir(app_data_root)
    except OSError:
        return 0
    total = 0
    with entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _check_min_free(config: Config) -> tuple[int, int] | None:
    """Return ``(free, min_free)`` if a threshold is set and free space is below it, else None."""
    min_free = storage_min_free_bytes(config)
    if min_free is None:
        return None
    free = disk_free_bytes(config)
    if free < min_free:
        return free, min_free
    return None


def storage_low(config: Config) -> bool:
    """Return True if a min-free threshold is set and free space is below it."""
    return _check_min_free(config) is not None


def check_before_deploy(config: Config) -> None:
    """Pre-deploy storage check: raise if free space is below the configured minimum."""
    result = _check_min_free(config)
    if result is not None:
        free, min_free = result
        raise RuntimeError(f"Storage too low ({format_bytes(free)} free, {format_bytes(min_free)} required)")


def storage_status(config: Config) -> dict[str, object]:
    """Return disk totals and usage for the System page / API.

    Both persistent and temporary data live on the same disk (under
    ``data_root_dir``), so we report a single unified disk metric.
    """
    _ensure_storage_roots(config)

    disk = shutil.disk_usage(config.data_root_dir)

    min_free = storage_min_free_bytes(config)

    # The app_data total is derived from the per-app walk rather than walked
    # again — these trees are large enough that a second pass is noticeable.
    per_app = per_app_usage(config)

    image_storage = container_image_storage_bytes()

    return {
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        "openhost_data_used_bytes": openhost_data_usage_bytes(config),
        "app_data_used_bytes": sum(per_app.values()) + _app_data_loose_file_bytes(config),
        "build_cache_bytes": None if image_storage is None else image_storage[0],
        "build_cache_reclaimable_bytes": None if image_storage is None else image_storage[1],
        "per_app": per_app,
        "storage_min_free_bytes": min_free,
        "storage_low": storage_low(config),
        "guard_paused": is_guard_paused(),
    }


# ---------------------------------------------------------------------------
# Storage guard — enforcement loop
# ---------------------------------------------------------------------------


def _stop_app_process_safe(row: sqlite3.Row) -> None:
    """Call stop_app_process, catching errors."""
    try:
        stop_app_process(row)
    except Exception:
        logger.exception("Failed to stop app %s", row["name"])


def enforce_storage_guard(config: Config) -> None:
    """Enforce minimum free disk space.

    If free space is below the threshold and the guard is not paused,
    stops all running apps.
    """
    result = _check_min_free(config)
    if result is None:
        return

    free, min_free = result
    logger.warning(
        "Storage low (%s free, %s required)",
        format_bytes(free),
        format_bytes(min_free),
    )

    if is_guard_paused():
        logger.info("Storage guard is paused; skipping app shutdown despite low storage")
        return

    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT * FROM apps WHERE status IN ('running', 'starting')").fetchall()
        if not rows:
            return

        for row in rows:
            detail = "Storage too low. Free space by removing app data or resizing disks."
            logger.warning("Stopping app %s due to low storage", row["name"])
            _stop_app_process_safe(row)
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ?, container_id = NULL WHERE app_id = ?",
                (detail, row["app_id"]),
            )
            db.commit()
    finally:
        db.close()


def _storage_guard_loop(config: Config) -> None:
    while True:
        try:
            enforce_storage_guard(config)
        except Exception:
            logger.exception("Storage guard check failed")
        time.sleep(_STORAGE_GUARD_INTERVAL_SECONDS)


def start_storage_guard(config: Config) -> None:
    """Start the storage guard daemon thread (once per db_path).

    Only starts if a minimum free space threshold is configured.
    """
    if storage_min_free_bytes(config) is None:
        return
    db_key = os.path.abspath(config.db_path)
    if db_key in _guard_db_paths:
        return
    _guard_db_paths.add(db_key)
    threading.Thread(target=_storage_guard_loop, args=(config,), daemon=True).start()
