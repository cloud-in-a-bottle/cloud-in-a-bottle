"""Apps that track their default branch (no ref pinned) and set prebuilt_image install at the newest release tag on that
branch; a pinned ref builds as before."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from compute_space.core.apps import clone_and_read_manifest
from compute_space.core.manifest import parse_manifest
from compute_space.core.releases import ReleaseError
from compute_space.core.releases import checkout_newest_release
from compute_space.core.releases import tracks_default_branch

_MANIFEST = '[app]\nname = "myapp"\nversion = "{version}"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
_PREBUILT = 'prebuilt_image = "registry.cloudinabottle.org/myapp"\n'


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    [
        ("https://github.com/x/app", True),
        ("https://github.com/x/app@main", False),
        ("https://github.com/x/app@v1.0.0", False),
        ("https://github.com/x/app@" + "a" * 40, False),
        (None, False),
    ],
)
def test_only_a_refless_repo_url_tracks_the_default_branch(repo_url: str | None, expected: bool) -> None:
    assert tracks_default_branch(repo_url) is expected


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


class _Origin:
    def __init__(self, path: Path, prebuilt: bool = True) -> None:
        self.path = path
        self.prebuilt = prebuilt
        path.mkdir()
        _git(path, "init", "-b", "main")

    def commit(self, version: str, tag: str | None = None) -> str:
        (self.path / "cloudinabottle.toml").write_text(
            _MANIFEST.format(version=version) + (_PREBUILT if self.prebuilt else "")
        )
        _git(self.path, "add", ".")
        _git(self.path, "commit", "-m", version)
        if tag:
            _git(self.path, "tag", tag)
        return _git(self.path, "rev-parse", "HEAD")


def _clone(url: str) -> Path:
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(url))
    assert error is None, error
    assert clone_dir is not None
    return Path(clone_dir)


def test_install_checks_out_the_newest_release_not_the_branch_head(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    origin.commit("1.0.0", tag="v1.0.0")
    released = origin.commit("1.2.0", tag="v1.2.0")
    origin.commit("1.2.1")  # merged, but its image isn't released yet
    clone = _clone(f"file://{origin.path}")
    assert _git(clone, "rev-parse", "HEAD") == released
    assert parse_manifest(str(clone)).version == "1.2.0"


def test_install_compares_versions_numerically_and_ignores_candidates(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    origin.commit("1.9.0", tag="v1.9.0")
    newest = origin.commit("1.10.0", tag="v1.10.0")
    origin.commit("2.0.0-rc.1", tag="v2.0.0-rc.1")
    assert _git(_clone(f"file://{origin.path}"), "rev-parse", "HEAD") == newest


def test_install_ignores_release_tags_on_other_branches(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    on_main = origin.commit("1.0.0", tag="v1.0.0")
    _git(origin.path, "checkout", "-b", "experiment")
    origin.commit("9.0.0", tag="v9.0.0")
    _git(origin.path, "checkout", "main")
    assert _git(_clone(f"file://{origin.path}"), "rev-parse", "HEAD") == on_main


def test_install_fails_when_prebuilt_image_is_set_but_nothing_is_released(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    origin.commit("0.1.0")
    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin.path}"))
    assert manifest is None and clone_dir is None
    assert error is not None and "no vX.Y.Z release tag" in error


def test_install_without_prebuilt_image_takes_the_branch_head(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin", prebuilt=False)
    origin.commit("1.0.0", tag="v1.0.0")
    head = origin.commit("1.0.1")
    assert _git(_clone(f"file://{origin.path}"), "rev-parse", "HEAD") == head


def test_pin_to_the_default_branch_is_a_pin_like_any_other(tmp_path: Path) -> None:
    """``@main`` builds main's head: only an app with no ref follows releases."""
    origin = _Origin(tmp_path / "origin")
    origin.commit("1.0.0", tag="v1.0.0")
    head = origin.commit("1.0.1")
    assert _git(_clone(f"file://{origin.path}@main"), "rev-parse", "HEAD") == head


def test_pin_to_another_branch_ignores_prebuilt_image_and_tags(tmp_path: Path) -> None:
    """A pin to a branch that isn't the default builds its head as before: no release, no error."""
    origin = _Origin(tmp_path / "origin")
    origin.commit("1.0.0", tag="v1.0.0")
    _git(origin.path, "checkout", "-b", "experiment")
    head = origin.commit("1.1.0")
    _git(origin.path, "checkout", "main")
    url = f"file://{origin.path}@experiment"
    clone = _clone(url)
    assert _git(clone, "rev-parse", "HEAD") == head


def test_pin_to_a_tag_builds_that_tag(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    pinned = origin.commit("1.0.0", tag="v1.0.0")
    origin.commit("1.1.0", tag="v1.1.0")
    clone = _clone(f"file://{origin.path}@v1.0.0")
    assert _git(clone, "rev-parse", "HEAD") == pinned


def test_update_fetches_a_tag_created_after_its_commit_was_pulled(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    origin.commit("1.0.0", tag="v1.0.0")
    clone = _clone(f"file://{origin.path}")
    later = origin.commit("2.0.0")
    _git(clone, "fetch", "origin", "main")
    _git(clone, "checkout", "-fB", "main", "origin/main")  # the commit lands before CI tags it
    _git(origin.path, "tag", "v2.0.0", later)
    assert checkout_newest_release(str(clone), fetch_tags=True) == "v2.0.0"
    assert _git(clone, "rev-parse", "HEAD") == later


def test_a_plain_directory_has_no_releases(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="isn't a git checkout"):
        checkout_newest_release(str(tmp_path), fetch_tags=False)


def test_the_release_checkout_runs_no_hooks_from_the_app_repo(tmp_path: Path) -> None:
    origin = _Origin(tmp_path / "origin")
    origin.commit("1.0.0", tag="v1.0.0")
    clone = _clone(f"file://{origin.path}")
    later = origin.commit("2.0.0")
    _git(clone, "fetch", "origin", "main")
    _git(clone, "checkout", "-fB", "main", "origin/main")
    _git(origin.path, "tag", "v2.0.0", later)
    marker = tmp_path / "hook-ran"
    # The tag fetch runs reference-transaction and the checkout post-checkout, if hooks are enabled.
    for name in ("reference-transaction", "post-checkout"):
        hook = clone / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        hook.chmod(0o755)
    assert checkout_newest_release(str(clone), fetch_tags=True) == "v2.0.0"
    assert _git(clone, "rev-parse", "HEAD") == later
    assert not marker.exists()
