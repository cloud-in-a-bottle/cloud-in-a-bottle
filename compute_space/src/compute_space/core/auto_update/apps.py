from __future__ import annotations

import asyncio
import os
import re
from contextlib import closing
from datetime import UTC
from datetime import datetime
from pathlib import Path

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.app_update_review import pending_update_review
from compute_space.core.apps import app_container_log_path
from compute_space.core.apps import app_log_path
from compute_space.core.apps import git_pull
from compute_space.core.apps import reload_app_background
from compute_space.core.auto_update.app_config import AppAutoUpdateLastRun
from compute_space.core.auto_update.app_config import AppUpdateOutcome
from compute_space.core.auto_update.app_config import AppUpdateResult
from compute_space.core.auto_update.app_config import record_app_last_run
from compute_space.core.containers import archive_old_log
from compute_space.core.containers import log_timestamp
from compute_space.core.containers import stop_app_process
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import is_dirty
from compute_space.core.git_ops import is_github_repo_url
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.git_ops import reset_hard
from compute_space.core.logging import logger
from compute_space.core.oauth import OAuthRequired
from compute_space.core.oauth import get_oauth_token
from compute_space.db import get_db

# A `url@ref` pinned to a commit never moves, so there is nothing to update to.
_COMMIT_REF_RE = re.compile(r"[0-9a-f]{7,40}")


async def _pull(repo_path: str, app_name: str, repo_url: str) -> str | None:
    """Pull the app's upstream as a manual update does, using a stored GitHub grant if the anonymous pull fails.
    Returns the error, or None on success. Never asks the owner to authorize: there's nobody to ask."""
    ok, err = await asyncio.to_thread(git_pull, repo_path, app_name, repo_url=repo_url)
    if ok:
        return None
    if not is_github_repo_url(repo_url):
        return err
    try:
        token = await get_oauth_token("github", ["repo"], return_to="/settings")
    except OAuthRequired:
        return f"{err} (if the repository is private, update the app once by hand to grant GitHub access)"
    except RuntimeError as e:
        return str(e)
    ok, err = await asyncio.to_thread(git_pull, repo_path, app_name, github_token=token, repo_url=repo_url)
    return None if ok else err


async def auto_update_app(app_id: str, config: Config) -> AppUpdateResult:
    """Update one app to its upstream's latest version, unless the update needs the owner's approval. Mirrors a manual
    update from the app's page: a refused update rolls the checkout back, and a failed rebuild leaves the app in error
    just as a failed manual update does."""
    with closing(get_db()) as db:
        row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    name = row["name"]
    repo_path = row["repo_path"]

    def result(outcome: AppUpdateOutcome, detail: str | None = None) -> AppUpdateResult:
        return AppUpdateResult(app_id=app_id, app_name=name, outcome=outcome, detail=detail)

    if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")):
        return result(AppUpdateOutcome.SKIPPED, "not installed from a git repository")
    # A stopped app stays stopped, and one in error or mid-reload is the owner's to look at.
    if row["status"] != "running":
        return result(AppUpdateOutcome.SKIPPED, f"not running ({row['status']})")
    repo_url = row["repo_url"] or ""
    try:
        _, ref = parse_repo_url(repo_url)
    except ValueError as e:
        return result(AppUpdateOutcome.SKIPPED, str(e))
    if ref is not None and _COMMIT_REF_RE.fullmatch(ref):
        return result(AppUpdateOutcome.SKIPPED, f"pinned to commit {ref}")
    # A manual update would discard local edits (it checks out with -f); don't do that unattended.
    if await is_dirty(Path(repo_path)):
        return result(AppUpdateOutcome.SKIPPED, "has local changes")
    with closing(get_db()) as db:
        archive_healthy = archive_backend.is_archive_dir_healthy(config, db)
    if not archive_healthy and archive_backend.manifest_requires_archive(row["manifest_raw"] or ""):
        return result(AppUpdateOutcome.SKIPPED, "needs the archive backend, which is not healthy")

    pre_sha = await get_head_sha(Path(repo_path))
    pull_error = await _pull(repo_path, name, repo_url)
    if pull_error is not None:
        await reset_hard(Path(repo_path), pre_sha)
        return result(AppUpdateOutcome.FAILED, f"git pull failed: {pull_error}")
    post_sha = await get_head_sha(Path(repo_path))
    if post_sha == pre_sha:
        return result(AppUpdateOutcome.UP_TO_DATE)

    review = await asyncio.to_thread(pending_update_review, app_id, repo_path, row["manifest_raw"])
    if review is not None:
        # As for a refused manual update: keep the checkout matching the running version, so a later plain reload
        # doesn't deploy the unapproved code.
        await reset_hard(Path(repo_path), pre_sha)
        return result(AppUpdateOutcome.NEEDS_APPROVAL, review.describe())

    log_file = app_log_path(name, config)
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        ts = log_timestamp(log_file)
        archive_old_log(log_file, ts=ts)
        archive_old_log(app_container_log_path(name, config), ts=ts)
        with open(log_file, "a") as lf:
            lf.write(f"automatic update {pre_sha[:8]} -> {post_sha[:8]}\n")
    except OSError as e:
        await reset_hard(Path(repo_path), pre_sha)
        return result(AppUpdateOutcome.FAILED, f"could not write update log: {e}")

    # Claim the reload as the manual path does, so a reload the owner starts meanwhile can't race this one.
    with closing(get_db()) as db:
        cursor = db.execute(
            "UPDATE apps SET status = 'building', container_id = NULL, error_message = NULL "
            "WHERE app_id = ? AND status NOT IN ('building', 'starting', 'removing')",
            (app_id,),
        )
        db.commit()
    if cursor.rowcount == 0:
        await reset_hard(Path(repo_path), pre_sha)
        return result(AppUpdateOutcome.SKIPPED, "already reloading")

    await asyncio.to_thread(stop_app_process, row)
    # Run in this task rather than a thread of its own, so apps update one at a time.
    await asyncio.to_thread(reload_app_background, app_id, repo_path, config)
    with closing(get_db()) as db:
        after = db.execute("SELECT status, error_message FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if after["status"] != "running":
        return result(AppUpdateOutcome.FAILED, after["error_message"] or f"{after['status']} after the update")
    return result(AppUpdateOutcome.UPDATED, f"{pre_sha[:8]} -> {post_sha[:8]}")


async def run_app_auto_updates() -> None:
    """Update every installed app that can be updated without the owner's approval, one at a time, and record each
    app's outcome. One app's failure doesn't stop the others."""
    config = get_config()
    now = datetime.now(UTC)
    with closing(get_db()) as db:
        apps = db.execute("SELECT app_id, name FROM apps ORDER BY name").fetchall()
    results: list[AppUpdateResult] = []
    for app in apps:
        try:
            app_result = await auto_update_app(app["app_id"], config)
        except Exception as e:
            logger.exception("automatic update of {} failed", app["name"])
            app_result = AppUpdateResult(
                app_id=app["app_id"], app_name=app["name"], outcome=AppUpdateOutcome.FAILED, detail=str(e)
            )
        logger.info("automatic app update: {} {} {}", app_result.app_name, app_result.outcome, app_result.detail or "")
        results.append(app_result)
    with closing(get_db()) as db:
        record_app_last_run(db, AppAutoUpdateLastRun(at=now, results=results))
