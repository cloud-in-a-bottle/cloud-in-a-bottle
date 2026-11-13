"""Releases of apps that set ``prebuilt_image``.

An app that tracks its repo's default branch (no ``@ref`` pinned) and sets ``prebuilt_image`` is installed and updated
to the newest release tag on that branch, and runs that commit's prebuilt image. CI only tags a commit once its image is
published, so a release normally has an image to pull; when it can't be fetched, the release is built locally instead
(see apps.prepare_app_image). A pinned ref (a branch, tag or commit) ignores all of this and
builds, as before.
"""

import os
import subprocess
from pathlib import Path

import git

from compute_space.core.git_ops import checkout_ref
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.manifest import AppManifest
from openhost_system_agent.git_commands import git_command
from openhost_system_agent.release_tags import newest_release


class ReleaseError(RuntimeError):
    pass


def tracks_default_branch(repo_url: str | None) -> bool:
    """Whether the app follows its repo's default branch: a repo URL with no ``@ref`` pinned."""
    if not repo_url:
        return False
    try:
        _, ref = parse_repo_url(repo_url)
    except ValueError:
        return False
    return ref is None


def follows_releases(repo_url: str | None, manifest: AppManifest) -> bool:
    """Whether the app checks out its newest release and runs that commit's prebuilt image instead of building."""
    return manifest.prebuilt_image is not None and tracks_default_branch(repo_url)


def _git(repo_path: str, args: list[str], timeout: int = 60) -> str:
    result = subprocess.run(git_command(*args), cwd=repo_path, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def checkout_newest_release(repo_path: str, fetch_tags: bool) -> str:
    """Check out the newest release tag reachable from HEAD (the default branch's head, freshly cloned or pulled) and
    return it. ``fetch_tags`` fetches origin's tags first: a pull only follows tags on newly fetched commits, and CI
    tags a commit after it lands. Raises :class:`ReleaseError` when there is no release: an app that sets
    ``prebuilt_image`` only ever deploys releases."""
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        raise ReleaseError("prebuilt_image is set, but the app isn't a git checkout, so it has no release tags")
    if fetch_tags:
        _git(repo_path, ["fetch", "--tags", "--force", "origin"])
    tag = newest_release(_git(repo_path, ["tag", "--list", "v*", "--merged", "HEAD"]).split())
    if tag is None:
        raise ReleaseError(
            "prebuilt_image is set, but the repo has no vX.Y.Z release tag on its default branch to install"
        )
    try:
        checkout_ref(Path(repo_path), tag)
    except git.GitCommandError as e:
        raise ReleaseError(f"Checking out release {tag} failed: {e}") from e
    if os.path.exists(os.path.join(repo_path, ".gitmodules")):
        _git(repo_path, ["submodule", "update", "--init", "--recursive"], timeout=300)
    return tag
