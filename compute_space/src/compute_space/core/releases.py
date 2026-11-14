"""Releases of apps that set ``[app].release_tag_format``.

An app that tracks its repo's default branch (no ``@ref`` pinned) and sets ``release_tag_format`` is installed and
updated to the newest release tag on that branch. If it also sets ``prebuilt_image`` it runs that commit's prebuilt
image (CI only tags a commit once its image is published; when it can't be fetched, the release is built locally
instead, see apps.prepare_app_image); otherwise it builds the release. A pinned ref (a branch, tag or commit) ignores
all of this and builds its head, as before.
"""

import os
import subprocess
from pathlib import Path
from typing import assert_never

import git

from compute_space.core.git_ops import checkout_ref
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import ReleaseTagFormat
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
    """Whether the app deploys its newest release rather than the head of its branch: it declares how its releases are
    tagged, and doesn't pin a ref."""
    return manifest.release_tag_format is not None and tracks_default_branch(repo_url)


def _git(repo_path: str, args: list[str], timeout: int = 60) -> str:
    result = subprocess.run(git_command(*args), cwd=repo_path, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def checkout_newest_release(repo_path: str, tag_format: ReleaseTagFormat, fetch_tags: bool) -> str:
    """Check out the newest release tag (in ``tag_format``) reachable from HEAD (the default branch's head, freshly
    cloned or pulled) and return it. ``fetch_tags`` fetches origin's tags first: a pull only follows tags on newly
    fetched commits, and CI tags a commit after it lands. Raises :class:`ReleaseError` when there is no release: an
    app that follows releases only ever deploys releases."""
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        raise ReleaseError("release_tag_format is set, but the app isn't a git checkout, so it has no release tags")
    if fetch_tags:
        _git(repo_path, ["fetch", "--tags", "--force", "origin"])
    match tag_format:
        case ReleaseTagFormat.SEMVER:
            tag = newest_release(_git(repo_path, ["tag", "--list", "v*", "--merged", "HEAD"]).split())
        case _:
            assert_never(tag_format)
    if tag is None:
        raise ReleaseError(
            f'release_tag_format is "{tag_format}", but the repo has no vX.Y.Z release tag on its default branch to '
            "install"
        )
    try:
        checkout_ref(Path(repo_path), tag)
    except git.GitCommandError as e:
        raise ReleaseError(f"Checking out release {tag} failed: {e}") from e
    if os.path.exists(os.path.join(repo_path, ".gitmodules")):
        _git(repo_path, ["submodule", "update", "--init", "--recursive"], timeout=300)
    return tag
