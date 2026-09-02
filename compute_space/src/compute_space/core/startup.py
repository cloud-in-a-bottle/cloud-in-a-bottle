import os
import sqlite3
import threading
import time

from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.apps import resume_primary_domain_app_restarts
from compute_space.core.apps import start_app_process
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import drop_docker_build_cache
from compute_space.core.containers import is_container_running
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.domains import INTERRUPTED_APP_REMOVAL_MESSAGE
from compute_space.core.domains import PRIMARY_DOMAIN_APP_RESTART_MARKER
from compute_space.core.domains import pending_primary_domain_restart_app_ids
from compute_space.core.logging import logger

# UTC timestamp captured at module import.  check_app_status uses this to
# distinguish rows inserted by this process (whose build threads are still
# running) from rows abandoned by a previous process (which should be swept).
_PROCESS_START_UTC = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def check_app_status(
    config: Config,
    recover_quiesced_archive_apps: bool = False,
    restart_synchronously: bool = False,
) -> None:
    """On startup, verify apps that should be up are still alive.

    Covers 'running' apps plus apps left mid-restart in 'starting'/'building':
    a reboot kills every container, and if a prior restart sweep was interrupted
    (e.g. the service restarted mid-rebuild) those apps stay in 'starting'.
    Looking only at 'running' would strand them forever.

    Apps that need rebuilding are restarted sequentially in a single background
    thread to avoid concurrent image builds against the same containers-storage
    instance.
    """
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    apps_to_restart: list[str] = []
    try:
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE status = 'removing'",
            (INTERRUPTED_APP_REMOVAL_MESSAGE,),
        )
        primary_domain_restart_ids = set(pending_primary_domain_restart_app_ids(db))
        statuses = "status IN ('running', 'starting', 'building')"
        if recover_quiesced_archive_apps:
            statuses += " OR (status = 'error' AND container_id IS NOT NULL)"
        rows = db.execute(f"SELECT * FROM apps WHERE {statuses}").fetchall()
        for row in rows:
            if row["app_id"] in primary_domain_restart_ids:
                continue
            if row["status"] == "error" and not archive_backend.manifest_uses_archive(row["manifest_raw"] or ""):
                continue
            alive = bool(row["container_id"]) and is_container_running(row["container_id"])

            if alive:
                # Container survived, or a prior sweep restarted it but the
                # status never advanced past 'starting'/'building'. Heal it.
                if row["status"] in ("starting", "building"):
                    db.execute(
                        "UPDATE apps SET status = 'running' WHERE app_id = ?",
                        (row["app_id"],),
                    )
                continue

            repo_path = row["repo_path"]
            if not repo_path or not os.path.isdir(repo_path):
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (
                        f"Cannot restart: repo path missing ({repo_path})",
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
                if (
                    row["error_message"] != PRIMARY_DOMAIN_APP_RESTART_MARKER
                    and row["created_at"] >= _PROCESS_START_UTC
                ):
                    db.execute(
                        "UPDATE apps SET status = 'starting' WHERE app_id = ?",
                        (row["app_id"],),
                    )
                    continue

            db.execute(
                "UPDATE apps SET status = 'starting' WHERE app_id = ?",
                (row["app_id"],),
            )
            apps_to_restart.append(row["app_id"])

        # Recover apps whose build corrupted containers-storage onto the same
        # serial rebuild path (which exists precisely to avoid that corruption).
        if primary_domain_restart_ids:
            logger.info("Deferring build-cache recovery until primary-domain app restarts finish")
        else:
            apps_to_restart.extend(_recover_cache_corrupt_apps(db))

        db.commit()
    finally:
        db.close()

    if apps_to_restart:
        if restart_synchronously:
            _restart_apps_sequential(apps_to_restart, config)
        else:
            threading.Thread(
                target=_restart_apps_sequential,
                args=(apps_to_restart, config),
                daemon=True,
            ).start()
    resume_primary_domain_app_restarts(config)


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


def resume_deferred_cache_recovery(config: Config) -> None:
    """Run cache-corruption recovery after a startup primary-domain queue drains."""
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    try:
        if pending_primary_domain_restart_app_ids(db):
            return
        apps_to_restart = _recover_cache_corrupt_apps(db)
        db.commit()
    finally:
        db.close()
    if apps_to_restart:
        threading.Thread(
            target=_restart_apps_sequential,
            args=(apps_to_restart, config),
            daemon=True,
        ).start()


def _restart_apps_sequential(app_ids: list[str], config: Config) -> None:
    """Rebuild and restart apps one at a time in a background thread."""
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        for app_id in app_ids:
            try:
                start_app_process(app_id, db, config)
                logger.info("Rebuilt and restarted app {}", app_id)
            except Exception as e:
                logger.exception("Failed to rebuild app {}", app_id)
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
