"""v17 drops the ``@<branch>`` openhost pinned onto apps tracking their default branch, and nothing else."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from compute_space.db.connection import init_db
from compute_space.db.versioned.migrations.v0017_unpin_default_branch import Migration0017UnpinDefaultBranch
from compute_space.db.versioned.migrations.v0017_unpin_default_branch import unpinned_repo_url

_URL = "https://github.com/cloud-in-a-bottle/bottled-miniflux"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone whose origin/HEAD says master, as for an install made before master was renamed to main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "master")
    _git(origin, "commit", "--allow-empty", "-m", "init")
    _git(origin, "tag", "v1.0.0")
    _git(origin, "branch", "feature")
    path = tmp_path / "clone"
    _git(tmp_path, "clone", f"file://{origin}", str(path))
    _git(origin, "branch", "-m", "master", "main")  # the rename the clone hasn't seen
    return path


def test_pin_to_the_recorded_default_branch_is_dropped(clone: Path) -> None:
    assert unpinned_repo_url(f"{_URL}@master", str(clone)) == _URL


@pytest.mark.parametrize("ref", ["feature", "v1.0.0", "0" * 40, "main"])
def test_other_pins_are_kept(clone: Path, ref: str) -> None:
    assert unpinned_repo_url(f"{_URL}@{ref}", str(clone)) is None


@pytest.mark.parametrize(
    "repo_url",
    [_URL, "https://user@github.com/cloud-in-a-bottle/bottled-miniflux", "git@github.com:cloud-in-a-bottle/app.git"],
)
def test_urls_without_a_ref_are_left_alone(clone: Path, repo_url: str) -> None:
    assert unpinned_repo_url(repo_url, str(clone)) is None


def test_missing_repo_keeps_the_pin(tmp_path: Path) -> None:
    assert unpinned_repo_url(f"{_URL}@master", str(tmp_path / "gone")) is None


def test_missing_origin_head_keeps_the_pin(clone: Path) -> None:
    (clone / ".git" / "refs" / "remotes" / "origin" / "HEAD").unlink()
    assert unpinned_repo_url(f"{_URL}@master", str(clone)) is None


def test_migration_rewrites_only_matching_rows_and_is_idempotent(clone: Path, tmp_path: Path) -> None:
    db_path = str(tmp_path / "router.db")
    init_db(db_path)
    rows = {
        "a1": (str(clone), f"{_URL}@master"),
        "a2": (str(clone), f"{_URL}@feature"),
        "a3": (str(tmp_path / "gone"), f"{_URL}@master"),
        "a4": (str(clone), None),
    }
    db = sqlite3.connect(db_path)
    try:
        for port, (app_id, (repo_path, repo_url)) in enumerate(rows.items(), start=21701):
            db.execute(
                "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status) "
                "VALUES (?, ?, '1.0', ?, ?, ?, 'running')",
                (app_id, f"app-{app_id}", repo_path, repo_url, port),
            )
        db.commit()
        for _ in range(2):
            Migration0017UnpinDefaultBranch().up(db)
        stored = dict(db.execute("SELECT app_id, repo_url FROM apps").fetchall())
    finally:
        db.close()
    assert stored == {"a1": _URL, "a2": f"{_URL}@feature", "a3": f"{_URL}@master", "a4": None}
