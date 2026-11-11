"""v17: drop the ``@<branch>`` openhost used to pin onto apps that track their repo's default branch.

Openhost used to record the branch a refless install landed on (``url@main``), so those apps stopped following the
default branch: after a rename (eg master to main) their pinned branch no longer exists and every update fails. Apps
without a ref now track the remote's default branch. This drops a pin when it names the branch the app repo's local
``refs/remotes/origin/HEAD`` points at, ie the default branch as of the last fetch (which still says ``master`` after
a rename). Every other pin (another branch, a tag, a commit) is the owner's choice and is left alone, as is any app
whose repo or origin/HEAD can't be read.

Python migration (not SQL-file) because it reads each app's repo on disk. It's self-contained rather than calling
openhost's git helpers, so later changes to those can't change what this migration did.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from compute_space.db.versioned.base import Migration

_ORIGIN_HEAD_PREFIX = "ref: refs/remotes/origin/"


def _origin_head_branch(repo_path: str) -> str | None:
    """The branch ``refs/remotes/origin/HEAD`` points at, read from the file (symbolic refs are never packed)."""
    try:
        content = (Path(repo_path) / ".git" / "refs" / "remotes" / "origin" / "HEAD").read_text().strip()
    except OSError:
        return None
    if not content.startswith(_ORIGIN_HEAD_PREFIX):
        return None
    return content.removeprefix(_ORIGIN_HEAD_PREFIX) or None


def unpinned_repo_url(repo_url: str, repo_path: str) -> str | None:
    """``repo_url`` without its ``@<branch>`` when that branch is the repo's recorded default; otherwise None."""
    base_url, sep, ref = repo_url.rpartition("@")
    # No ref, or the "@" belongs to userinfo or an SCP-style SSH URL (git@host:owner/repo) rather than a ref suffix.
    if not sep or not base_url or not ref or "/" in ref or ":" in ref:
        return None
    if "/" not in base_url:
        return None
    return base_url if _origin_head_branch(repo_path) == ref else None


class Migration0017UnpinDefaultBranch(Migration):
    version = 17

    def up(self, db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT app_id, repo_path, repo_url FROM apps WHERE repo_url IS NOT NULL").fetchall()
        for app_id, repo_path, repo_url in rows:
            unpinned = unpinned_repo_url(repo_url, repo_path)
            if unpinned is not None:
                db.execute("UPDATE apps SET repo_url = ? WHERE app_id = ?", (unpinned, app_id))
