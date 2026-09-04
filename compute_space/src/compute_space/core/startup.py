import os
import sqlite3
import threading
import time

from compute_space.config import Config
from compute_space.core.apps import restart_app_process
from compute_space.core.apps import start_app_process
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import drop_docker_build_cache
from compute_space.core.containers import image_exists
from compute_space.core.containers import is_container_running
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.logging import logger

# UTC timestamp captured at module import.  check_app_status uses this to
# distinguish rows inserted by this process (whose build threads are still
# running) from rows abandoned by a previous process (which should be swept).
_PROCESS_START_UTC = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def check_app_status(config: Config) -> None:
    """On startup, verify apps that should be up are still alive.

    Covers 'running' apps plus apps left mid-restart in 'starting'/'building':
    a reboot kills every container, and if a prior restart sweep was interrupted
    (e.g. the service restarted mid-rebuild) those apps stay in 'starting'.
    Looking only at 'running' would strand them forever.

    Recovery runs sequentially in one background thread. Ordinary restarts reuse
    the tagged image; interrupted builds and missing images rebuild safely.
    """
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    apps_to_recover: list[tuple[str, bool]] = []
    try:
        rows = db.execute("SELECT * FROM apps WHERE status IN ('running', 'starting', 'building')").fetchall()
        for row in rows:
            alive = bool(row["container_id"]) and is_container_running(row["container_id"])

            # A newly inserted row belongs to this process's deploy thread. It
            # is not durable restart intent yet, even if podman is already up.
            if row["status"] == "starting" and row["created_at"] >= _PROCESS_START_UTC:
                continue

            if alive and row["status"] != "starting":
                # A completed build may have reached podman before its final DB
                # update. A durable starting state, however, is restart intent.
                if row["status"] == "building":
                    db.execute(
                        "UPDATE apps SET status = 'running' WHERE app_id = ?",
                        (row["app_id"],),
                    )
                continue

            repo_path = row["repo_path"]
            has_repo = bool(repo_path) and os.path.isdir(repo_path)
            has_manifest = bool(row["manifest_raw"]) or has_repo
            image_tag = f"openhost-{row['name']}:latest"
            needs_build = row["status"] == "building" or not image_exists(image_tag)
            if not has_manifest or (needs_build and not has_repo):
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (
                        f"Cannot recover: repo path missing ({repo_path})",
                        row["app_id"],
                    ),
                )
                continue

            if not row["container_id"]:
                # No container yet — run_container() hadn't been called when the
                # process was killed.  Rows created after this process started have
                # an active deploy_app_background thread still running; mark them
                # starting so the dashboard reflects that, but don't queue a second
                # build.
                if row["created_at"] >= _PROCESS_START_UTC:
                    db.execute(
                        "UPDATE apps SET status = 'starting' WHERE app_id = ?",
                        (row["app_id"],),
                    )
                    continue

            db.execute(
                "UPDATE apps SET status = 'starting' WHERE app_id = ?",
                (row["app_id"],),
            )
            apps_to_recover.append((row["app_id"], needs_build))

        # Recover apps whose build corrupted containers-storage onto the same
        # serial rebuild path (which exists precisely to avoid that corruption).
        apps_to_recover.extend((app_id, True) for app_id in _recover_cache_corrupt_apps(db))

        db.commit()
    finally:
        db.close()

    if apps_to_recover:
        threading.Thread(
            target=_restart_apps_sequential,
            args=(apps_to_recover, config),
            daemon=True,
        ).start()


def _recover_cache_corrupt_apps(db: sqlite3.Connection) -> list[str]:
    """Prune the build cache and return app_ids to rebuild, for apps that
    failed with a cache-corruption marker.

    A plain retry can't recover — the corruption is cached — so the cache is
    dropped before the caller rebuilds these serially.  No attempt
    bookkeeping is needed to avoid looping: recovery clears the app's error,
    so it only reappears here if a serial, freshly-pruned rebuild reproduced
    the corruption, which the concurrency that causes it can't.  The caller
    commits ``db``.
    """
    rows = db.execute(
        "SELECT app_id, repo_path FROM apps WHERE status = 'error' AND error_message LIKE ?",
        (f"%{BUILD_CACHE_CORRUPT_MARKER}%",),
    ).fetchall()
    corrupt = [r["app_id"] for r in rows if r["repo_path"] and os.path.isdir(r["repo_path"])]
    if not corrupt:
        return []

    logger.warning(
        "containers-storage cache corruption in {} app(s); dropping build cache and rebuilding serially: {}",
        len(corrupt),
        corrupt,
    )
    try:
        output = drop_docker_build_cache()
        logger.info("dropped build cache before serial rebuild: {}", output)
    except Exception as e:
        # Rebuild anyway — a partial prune plus a fresh serial build often
        # still recovers.
        logger.error("failed to drop build cache during corruption recovery: {}", e)

    for app_id in corrupt:
        db.execute(
            "UPDATE apps SET status = 'starting', error_message = NULL WHERE app_id = ?",
            (app_id,),
        )
    return corrupt


def _restart_apps_sequential(apps: list[tuple[str, bool]], config: Config) -> None:
    """Recover apps one at a time, rebuilding only when required."""
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        for app_id, needs_build in apps:
            try:
                if needs_build:
                    start_app_process(app_id, db, config)
                else:
                    restart_app_process(app_id, db, config)
                logger.info("Recovered app {} ({})", app_id, "rebuilt" if needs_build else "restarted")
            except Exception as e:
                logger.exception("Failed to recover app {}", app_id)
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (str(e), app_id),
                )
                db.commit()
    finally:
        db.close()


def retry_pending_default_apps(config: Config) -> None:
    """Retry failed default-app installs on each boot."""
    db = sqlite3.connect(config.db_path)
    try:
        try:
            deploy_default_apps(config, db)
        except Exception as exc:
            logger.error("default_apps retry on startup raised: {}", exc)
    finally:
        db.close()
