"""Tests that ``clone_and_read_manifest`` can clone at a commit-hash ref.

``git clone --branch`` only accepts a branch or tag name, so a bare commit
hash must be cloned as the default branch and checked out afterwards.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from compute_space.core.apps import clone_and_read_manifest

_MANIFEST = '[app]\nname = "myapp"\nversion = "{version}"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).stdout.strip()


def _two_commit_origin(tmp_path: Path) -> tuple[Path, str, str]:
    """An origin repo with v1 then v2 of openhost.toml. Returns (origin, v1_sha, v2_sha)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "openhost.toml").write_text(_MANIFEST.format(version="1.0.0"))
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "v1")
    v1 = _git(origin, "rev-parse", "HEAD")
    (origin / "openhost.toml").write_text(_MANIFEST.format(version="2.0.0"))
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "v2")
    v2 = _git(origin, "rev-parse", "HEAD")
    return origin, v1, v2


def test_clone_at_full_commit_hash(tmp_path: Path) -> None:
    origin, v1, v2 = _two_commit_origin(tmp_path)
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}@{v1}"))
    assert error is None
    assert clone_dir is not None
    assert manifest is not None and manifest.version == "1.0.0"
    assert _git(Path(clone_dir), "rev-parse", "HEAD") == v1


def test_clone_at_short_commit_hash(tmp_path: Path) -> None:
    origin, v1, v2 = _two_commit_origin(tmp_path)
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}@{v1[:8]}"))
    assert error is None
    assert manifest is not None and manifest.version == "1.0.0"
    assert clone_dir is not None
    assert _git(Path(clone_dir), "rev-parse", "HEAD") == v1


def test_clone_at_tag_still_works(tmp_path: Path) -> None:
    """Regression: tag refs still go through --branch and check out correctly."""
    origin, v1, v2 = _two_commit_origin(tmp_path)
    _git(origin, "tag", "v1.0.0", v1)
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}@v1.0.0"))
    assert error is None
    assert manifest is not None and manifest.version == "1.0.0"
    assert clone_dir is not None
    assert _git(Path(clone_dir), "rev-parse", "HEAD") == v1


def test_clone_at_branch(tmp_path: Path) -> None:
    """A branch ref goes through --branch and checks out an attached branch."""
    origin, v1, v2 = _two_commit_origin(tmp_path)
    _git(origin, "branch", "stable", v1)
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}@stable"))
    assert error is None
    assert manifest is not None and manifest.version == "1.0.0"
    assert clone_dir is not None
    assert _git(Path(clone_dir), "symbolic-ref", "-q", "HEAD") == "refs/heads/stable"


def test_hex_named_branch_resolves_to_branch(tmp_path: Path) -> None:
    """A branch whose name looks like a short commit hash is still resolved as a
    branch (the remote is asked, not the ref's shape), so its content is used."""
    origin, v1, v2 = _two_commit_origin(tmp_path)
    _git(origin, "branch", "abcdef1", v1)  # 7 hex chars — indistinguishable by shape
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}@abcdef1"))
    assert error is None
    assert manifest is not None and manifest.version == "1.0.0"
    assert clone_dir is not None
    assert _git(Path(clone_dir), "symbolic-ref", "-q", "HEAD") == "refs/heads/abcdef1"


def test_clone_default_branch_without_ref(tmp_path: Path) -> None:
    origin, _v1, v2 = _two_commit_origin(tmp_path)
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}"))
    assert error is None
    assert manifest is not None and manifest.version == "2.0.0"
    assert clone_dir is not None
    assert _git(Path(clone_dir), "rev-parse", "HEAD") == v2
