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
import shlex
import subprocess
from pathlib import Path

import pytest

from compute_space.core import apps
from compute_space.core.apps import clone_and_read_manifest
from compute_space.core.apps import git_pull
from compute_space.core.git_ops import github_token_git_config
from compute_space.core.git_ops import hard_checkout_ref
from compute_space.core.git_ops import reset_hard
from openhost_system_agent.git_commands import HooklessRepo

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


def _plant_hook(directory: Path, marker: Path, name: str = "post-checkout") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / name
    hook.write_text(f"#!/bin/sh\nprintf 'ran\\n' >> {shlex.quote(str(marker))}\n")
    hook.chmod(0o755)


@pytest.mark.parametrize("custom_hooks_path", [False, True])
def test_update_disables_main_and_submodule_hooks(tmp_path: Path, custom_hooks_path: bool) -> None:
    origin, sub_origin = _make_submodule_origin(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--recurse-submodules", f"file://{origin}", str(clone))
    marker = tmp_path / "hook-ran"
    for repo, git_dir in ((clone, clone / ".git"), (clone / "sub", clone / ".git/modules/sub")):
        hooks = git_dir / ("custom-hooks" if custom_hooks_path else "hooks")
        _plant_hook(hooks, marker)
        if custom_hooks_path:
            _git(repo, "config", "core.hooksPath", str(hooks))
        _git(repo, "checkout", "--detach", "HEAD")
        assert marker.exists(), "The control checkout must execute the planted hook"
        marker.unlink()

    _bump_submodule(origin, sub_origin, "v2")
    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@main")
    assert ok, err
    assert (clone / "sub/sub.txt").read_text() == "v2"
    assert not marker.exists()
    # Repeat without an upstream change: forced checkouts still execute hooks by default.
    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@main")
    assert ok, err
    assert not marker.exists()


def test_clone_disables_template_hooks_in_nested_submodules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin, sub_origin = _make_submodule_origin(tmp_path)
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    _git(leaf, "init", "-b", "main")
    (leaf / "leaf.txt").write_text("nested")
    _git(leaf, "add", ".")
    _git(leaf, "commit", "-m", "leaf")
    _git(sub_origin, "submodule", "add", f"file://{leaf}", "leaf")
    _git(sub_origin, "commit", "-m", "add leaf")
    _bump_submodule(origin, sub_origin, "v2")
    template = tmp_path / "template"
    marker = tmp_path / "template-hook-ran"
    _plant_hook(template / "hooks", marker)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))

    manifest, clone_dir, error = asyncio.run(clone_and_read_manifest(f"file://{origin}"))
    assert error is None, error
    assert manifest is not None and clone_dir is not None
    clone = Path(clone_dir)
    assert (clone / "sub/leaf/leaf.txt").read_text() == "nested"
    assert not marker.exists()
    # Verify the nested clone actually received the executable template hook.
    _git(clone / "sub/leaf", "checkout", "--detach", "HEAD")
    assert marker.exists()
    marker.unlink()
    (leaf / "leaf.txt").write_text("nested v2")
    _git(leaf, "commit", "-am", "leaf v2")
    _git(sub_origin / "leaf", "pull", "origin", "main")
    _git(sub_origin, "commit", "-am", "bump leaf")
    _bump_submodule(origin, sub_origin, "v3")
    ok, error = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@main")
    assert ok, error
    assert (clone / "sub/leaf/leaf.txt").read_text() == "nested v2"
    assert not marker.exists()


def test_gitpython_operations_keep_hooks_disabled(tmp_path: Path) -> None:
    origin, _ = _make_submodule_origin(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", f"file://{origin}", str(clone))
    marker = tmp_path / "gitpython-hook-ran"
    hooks = tmp_path / "custom-hooks"
    for name in ("post-checkout", "post-index-change", "reference-transaction"):
        _plant_hook(hooks, marker, name)
    _git(clone, "config", "core.hooksPath", str(hooks))
    _git(clone, "checkout", "--detach", "HEAD")
    assert marker.exists()
    marker.unlink()

    repo = HooklessRepo(clone)
    assert repo.git.config("--get", "core.hooksPath") == "/dev/null"
    assert repo.git.config("--get", "core.hooksPath") == "/dev/null"
    asyncio.run(hard_checkout_ref(clone, "main"))
    asyncio.run(hard_checkout_ref(clone, repo.head.commit.hexsha))
    asyncio.run(reset_hard(clone, repo.head.commit.hexsha))
    assert not marker.exists()
    assert repo.head.commit.hexsha == HooklessRepo(origin).head.commit.hexsha


def test_pull_fallback_disables_merge_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin, _ = _make_submodule_origin(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", f"file://{origin}", str(clone))
    marker = tmp_path / "merge-hook-ran"
    _plant_hook(clone / ".git/hooks", marker, "post-merge")
    (origin / "update.txt").write_text("update")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "update")
    monkeypatch.setattr(apps, "_remote_default_branch", lambda *_: (None, None))
    ok, error = git_pull(str(clone), "myapp")
    assert ok, error
    assert (clone / "update.txt").read_text() == "update"
    assert not marker.exists()
