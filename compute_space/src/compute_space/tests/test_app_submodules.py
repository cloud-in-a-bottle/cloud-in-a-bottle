"""Tests for git submodule support in app deploy and update.

Covers the initial clone (``clone_and_read_manifest`` recursing into
submodules) and the Update & Reload path (``git_pull`` materializing bumped
submodule pointers and initializing submodules added upstream) — without
which a submodule-based app would deploy fine but silently rebuild from
stale submodule content on every update.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from compute_space.core.apps import clone_and_read_manifest
from compute_space.core.apps import git_pull
from compute_space.core.git_ops import github_token_git_config

MANIFEST = '[app]\nname = "myapp"\nversion = "0.1.0"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'


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


@pytest.fixture(autouse=True)
def _allow_file_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Modern git refuses file-protocol submodules by default; allow them for
    the local fixture repos (real deploys use https)."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _make_submodule_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A superproject (with cloudinabottle.toml) containing one submodule at sub/."""
    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    _git(sub_origin, "init", "-b", "main")
    (sub_origin / "sub.txt").write_text("v1")
    _git(sub_origin, "add", ".")
    _git(sub_origin, "commit", "-m", "sub v1")

    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "cloudinabottle.toml").write_text(MANIFEST)
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "init")
    _git(origin, "submodule", "add", f"file://{sub_origin}", "sub")
    _git(origin, "commit", "-m", "add submodule")
    return origin, sub_origin


def _bump_submodule(origin: Path, sub_origin: Path, content: str) -> None:
    """Commit new content in the submodule origin and point the superproject
    at it."""
    (sub_origin / "sub.txt").write_text(content)
    _git(sub_origin, "commit", "-am", f"sub {content}")
    _git(origin / "sub", "pull", "origin", "main")
    _git(origin, "add", "sub")
    _git(origin, "commit", "-m", f"bump sub to {content}")


def test_clone_recurses_submodules(tmp_path: Path) -> None:
    """Deploy-time clone materializes submodule content."""
    origin, _ = _make_submodule_origin(tmp_path)

    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}"))
    assert error is None, error
    assert manifest is not None and manifest.name == "myapp"
    assert clone_dir is not None
    assert (Path(clone_dir) / "sub" / "sub.txt").read_text() == "v1"


def test_git_pull_updates_submodule_pointer(tmp_path: Path) -> None:
    """Update & Reload checks out the submodule commit the superproject now
    pins, so the rebuild sees the new content."""
    origin, sub_origin = _make_submodule_origin(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--recurse-submodules", f"file://{origin}", str(clone))
    assert (clone / "sub" / "sub.txt").read_text() == "v1"

    _bump_submodule(origin, sub_origin, "v2")

    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@main")
    assert ok, err
    assert (clone / "sub" / "sub.txt").read_text() == "v2"


def test_git_pull_inits_submodule_added_upstream(tmp_path: Path) -> None:
    """A submodule introduced after install is cloned on the next update."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "cloudinabottle.toml").write_text(MANIFEST)
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "init")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", f"file://{origin}", str(clone))

    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    _git(sub_origin, "init", "-b", "main")
    (sub_origin / "sub.txt").write_text("new")
    _git(sub_origin, "add", ".")
    _git(sub_origin, "commit", "-m", "sub")
    _git(origin, "submodule", "add", f"file://{sub_origin}", "sub")
    _git(origin, "commit", "-m", "add submodule")

    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@main")
    assert ok, err
    assert (clone / "sub" / "sub.txt").read_text() == "new"


def test_github_token_git_config() -> None:
    """The token rides only in ephemeral -c config, formatted for insteadOf."""
    assert github_token_git_config(None) == []
    assert github_token_git_config("tok123") == [
        "-c",
        "url.https://tok123@github.com/.insteadOf=https://github.com/",
    ]
