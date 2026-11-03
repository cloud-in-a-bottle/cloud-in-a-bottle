"""Storage usage helpers and the storage guard.

Reports disk usage totals and per-app breakdowns for the System page.
The storage guard runs as a daemon thread that periodically measures usage for
the System page and checks disk free space and stops apps when free space drops below ``storage_min_free_mb``.  It is
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
from concurrent.futures import ThreadPoolExecutor

from compute_space.config import Config
from compute_space.core.containers import app_container_writable_layer_bytes
from compute_space.core.containers import container_image_storage_bytes
from compute_space.core.containers import stop_app_process
from compute_space.core.logging import logger

_MIB = 1024 * 1024

# ---------------------------------------------------------------------------
# Guard state
# ---------------------------------------------------------------------------

_STORAGE_GUARD_INTERVAL_SECONDS = 300

_guard_paused: bool = False
_guard_lock = threading.Lock()
_guard_db_paths: set[str] = set()

# Walking app data and querying podman take seconds on a busy instance, so the
# guard loop measures usage in the background and requests read the latest
# snapshot.  Keyed by data_root_dir: ``(measured_at_epoch, usage)``.
_usage_snapshots: dict[str, tuple[float, dict[str, object]]] = {}
_usage_lock = threading.Lock()


def is_guard_paused() -> bool:
    with _guard_lock:
        return _guard_paused


def set_guard_paused(paused: bool) -> None:
    with _guard_lock:
        global _guard_paused
        _guard_paused = paused
    state = "paused" if paused else "resumed"
    logger.info("Storage guard {}", state)


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


def _dir_size_bytes(path: str, seen_inodes: set[tuple[int, int]] | None = None) -> int:
    """Return the disk space used by all files under *path*, in bytes.

    Counts allocated blocks (like ``du``) rather than apparent size, so sparse
    files aren't overcounted.  Hardlinked files are counted once per
    *seen_inodes* set.  Symlinks are not followed.
    """
    if seen_inodes is None:
        seen_inodes = set()
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
                    total += _dir_size_bytes(entry.path, seen_inodes)
                    continue
                st = entry.stat(follow_symlinks=False)
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)
                total += st.st_blocks * 512
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
    """Disk space of Cloud in a Bottle's own persistent data (router db, certs, identity keys)."""
    _ensure_storage_roots(config)
    seen: set[tuple[int, int]] = set()
    return sum(
        _dir_size_bytes(str(path), seen)
        for path in (config.openhost_data_path, os.path.join(config.persistent_data_dir, "vm_data"))
    )


def _subdir_sizes(root: str, seen_inodes: set[tuple[int, int]]) -> dict[str, int]:
    """Return ``{name: bytes}`` for each subdirectory of *root*."""
    if not os.path.isdir(root):
        return {}
    return {
        entry: _dir_size_bytes(path, seen_inodes)
        for entry in os.listdir(root)
        if os.path.isdir(path := os.path.join(root, entry))
    }


def _loose_file_bytes(root: str) -> int:
    """Disk space of files sitting directly in *root* (not inside a subdirectory)."""
    try:
        entries = os.scandir(root)
    except OSError:
        return 0
    total = 0
    with entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_blocks * 512
            except OSError:
                pass
    return total


def _add_sizes(into: dict[str, int], sizes: dict[str, int]) -> None:
    for app, size in sizes.items():
        into[app] = into.get(app, 0) + size


def per_app_usage(config: Config) -> dict[str, int]:
    """Return ``{app_name: bytes_used}`` across each app's app_data and app_temp_data dirs."""
    _ensure_storage_roots(config)
    seen: set[tuple[int, int]] = set()
    result = _subdir_sizes(os.path.join(config.persistent_data_dir, "app_data"), seen)
    _add_sizes(result, _subdir_sizes(os.path.join(config.temporary_data_dir, "app_temp_data"), seen))
    return result


def swap_file_bytes(config: Config) -> int:
    """Total size of swap files living on the data disk (swap partitions are excluded)."""
    try:
        with open("/proc/swaps") as f:
            lines = f.read().splitlines()[1:]
        data_dev = os.stat(config.data_root_dir).st_dev
    except OSError:
        return 0
    total = 0
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "file":
            continue
        # /proc/swaps escapes spaces in paths as \040
        path = fields[0].replace("\\040", " ")
        try:
            if os.stat(path).st_dev == data_dev:
                total += int(fields[2]) * 1024
        except (OSError, ValueError):
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


def _measure_usage(config: Config) -> dict[str, object]:
    """Walk app data and query podman for the slow part of the storage status."""
    _ensure_storage_roots(config)
    # The podman queries are slow (``ps --size`` walks every container layer),
    # so they run alongside the directory walks.
    with ThreadPoolExecutor(max_workers=2) as pool:
        layers_future = pool.submit(app_container_writable_layer_bytes)
        image_future = pool.submit(container_image_storage_bytes)
        per_app = per_app_usage(config)
        _add_sizes(per_app, layers_future.result())
        image_storage = image_future.result()

    return {
        "openhost_data_used_bytes": openhost_data_usage_bytes(config),
        "app_data_used_bytes": sum(per_app.values())
        + _loose_file_bytes(os.path.join(config.persistent_data_dir, "app_data")),
        "swap_file_bytes": swap_file_bytes(config),
        "build_cache_bytes": image_storage[0],
        "build_cache_reclaimable_bytes": image_storage[1],
        "per_app": per_app,
    }


def _store_usage_snapshot(config: Config) -> tuple[float, dict[str, object]]:
    entry = (time.time(), _measure_usage(config))
    _usage_snapshots[config.data_root_dir] = entry
    return entry


def refresh_usage_snapshot(config: Config) -> tuple[float, dict[str, object]]:
    """Measure usage now and store it as the latest snapshot."""
    with _usage_lock:
        return _store_usage_snapshot(config)


def _usage_snapshot(config: Config) -> tuple[float, dict[str, object]]:
    """Latest snapshot; measured on demand if the background loop hasn't produced one yet."""
    entry = _usage_snapshots.get(config.data_root_dir)
    if entry is not None:
        return entry
    with _usage_lock:
        entry = _usage_snapshots.get(config.data_root_dir)
        return entry if entry is not None else _store_usage_snapshot(config)


def storage_status(config: Config, refresh: bool = False) -> dict[str, object]:
    """Return disk totals and usage for the System page / API.

    Disk free and guard state are read live; the per-app breakdown comes from
    the latest background snapshot (``usage_measured_at``, epoch seconds)
    unless *refresh* forces a new measurement.  Both persistent and temporary
    data live on the same disk (under ``data_root_dir``), so we report a
    single unified disk metric.
    """
    _ensure_storage_roots(config)
    measured_at, usage = refresh_usage_snapshot(config) if refresh else _usage_snapshot(config)
    disk = shutil.disk_usage(config.data_root_dir)
    return {
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        **usage,
        "usage_measured_at": measured_at,
        "storage_min_free_bytes": storage_min_free_bytes(config),
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
        logger.exception("Failed to stop app {}", row["name"])


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
        "Storage low ({} free, {} required)",
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
            logger.warning("Stopping app {} due to low storage", row["name"])
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
            refresh_usage_snapshot(config)
        except Exception:
            logger.exception("Storage usage measurement failed")
        try:
            enforce_storage_guard(config)
        except Exception:
            logger.exception("Storage guard check failed")
        time.sleep(_STORAGE_GUARD_INTERVAL_SECONDS)


def start_storage_guard(config: Config) -> None:
    """Start the storage daemon thread (once per db_path).

    It refreshes the usage snapshot and, if a minimum free space threshold is
    configured, enforces it.
    """
    db_key = os.path.abspath(config.db_path)
    if db_key in _guard_db_paths:
        return
    _guard_db_paths.add(db_key)
    threading.Thread(target=_storage_guard_loop, args=(config,), daemon=True).start()
