from __future__ import annotations

import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import NoReturn

import git
from loguru import logger

from openhost_system_agent.protocol import DiffCommit
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import RemoteInfo

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent.parent.parent


def _ensure_repo_trusted() -> None:
    # The agent runs as root but the repo is owned by 'host'; tell git to trust
    # it so root operations don't fail with "detected dubious ownership".
    project = str(_PROJECT_DIR)
    g = git.Git()
    try:
        existing = g.config("--global", "--get-all", "safe.directory").split("\n")
    except git.GitCommandError:
        existing = []
    if project not in existing:
        g.config("--global", "--add", "safe.directory", project)


def _repo() -> git.Repo:
    _ensure_repo_trusted()
    return git.Repo(_PROJECT_DIR)


def _get_remote(repo: git.Repo) -> git.Remote:
    try:
        return repo.remote("origin")
    except (AttributeError, ValueError) as e:
        raise RuntimeError("remote 'origin' is not set") from e


_KNOWN_SCHEMES = {"http", "https", "ssh", "git", "file"}


def _strip_credentials(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        host_port = parsed.hostname or ""
        if parsed.port:
            host_port = f"{host_port}:{parsed.port}"
        return parsed._replace(netloc=host_port).geturl()
    return url


# ── Tag helpers ──────────────────────────────────────────────────────


# A release tag is "v" followed by dot-separated integers (v1, v1.2, v1.2.3).
# Other v-prefixed tags (e.g. v1.2.0-rc1) are ignored so version parsing
# can't crash on them.
_RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")


def _version_key(tag_name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag_name.lstrip("v").split("."))


def _get_sorted_tags(repo: git.Repo) -> list[str]:
    return sorted([t.name for t in repo.tags if _RELEASE_TAG.fullmatch(t.name)], key=_version_key)


def _current_tag(repo: git.Repo) -> str | None:
    try:
        result: str = repo.git.describe("--tags", "--exact-match", "HEAD")
    except git.GitCommandError:
        return None
    return result if _RELEASE_TAG.fullmatch(result) else None


def _latest_ancestor_tag(repo: git.Repo) -> str | None:
    try:
        result: str = repo.git.describe("--tags", "--abbrev=0", "HEAD")
    except git.GitCommandError:
        return None
    return result if _RELEASE_TAG.fullmatch(result) else None


# ── Target ref (pin to a branch/commit instead of the latest tag) ────


# Persisted in git config. When set, updates walk the release tags as usual but
# end on this ref (a branch tip or commit) instead of the latest tag. Unset →
# the latest tag is the destination. Kept in sync with apply_after_checkout.py.
_TARGET_REF_CONFIG = "openhost.target-ref"


def _get_target_ref(repo: git.Repo) -> str | None:
    try:
        value: str = repo.git.config("--get", _TARGET_REF_CONFIG)
    except git.GitCommandError:
        return None
    return value.strip() or None


def _set_target_ref(repo: git.Repo, ref: str | None) -> None:
    if ref:
        repo.git.config(_TARGET_REF_CONFIG, ref)
        return
    try:
        repo.git.config("--unset-all", _TARGET_REF_CONFIG)
    except git.GitCommandError:
        pass  # already unset


def _resolve_ref_sha(repo: git.Repo, ref: str) -> str | None:
    """Resolve a ref to a commit sha, preferring the fetched remote branch tip."""
    for candidate in (f"origin/{ref}", ref):
        try:
            return str(repo.git.rev_parse("--verify", "--quiet", f"{candidate}^{{commit}}"))
        except git.GitCommandError:
            continue
    return None


def _is_ancestor(repo: git.Repo, ancestor: str, descendant: str) -> bool:
    """True if ``ancestor`` is an ancestor of (or equal to) ``descendant``."""
    try:
        repo.git.merge_base("--is-ancestor", ancestor, descendant)
        return True
    except git.GitCommandError:
        return False


def _next_step(repo: git.Repo) -> str | None:
    """The next ref to check out while walking toward the destination, or None
    when already there.

    Release tags are walked in ascending version order as stepping stones, then
    the pinned target ref (if any) is the final hop. The destination is the
    pinned target when set, otherwise the latest release tag.

    When a target is pinned we must (1) treat "HEAD already at the target sha"
    as terminal *before* walking tags, and (2) only walk tags that are ancestors
    of the target. Otherwise a target that doesn't contain the newest tag (a
    branch cut from an older release, or a rollback pin) would oscillate forever
    between the newest tag and the target."""
    target = _get_target_ref(repo)
    target_sha = _resolve_ref_sha(repo, target) if target is not None else None

    # Pinned to a ref that no longer resolves (typo, or a branch/commit deleted
    # on the remote after it was pinned). Stop instead of silently walking to the
    # latest tag, which would abandon the operator's pin and jump the host onto a
    # release they explicitly opted out of. Mirrors fetch_updates.
    if target is not None and target_sha is None:
        raise RuntimeError(
            f"Pinned target ref '{target}' could not be resolved on the remote. "
            "Fix or clear the pin with 'set_remote' (a URL without an @ref clears it)."
        )

    # Terminal: already sitting on the pinned destination.
    if target_sha is not None and repo.head.commit.hexsha == target_sha:
        return None

    base = _current_tag(repo) or _latest_ancestor_tag(repo)
    later = [t for t in _get_sorted_tags(repo) if base is None or _version_key(t) > _version_key(base)]
    if target_sha is not None:
        # Only step through tags the pinned target actually contains, so the
        # walk stays monotonic toward the target instead of ping-ponging.
        later = [t for t in later if _is_ancestor(repo, t, target_sha)]
    if later:
        return later[0]

    # Tags exhausted (or none applicable): the pinned target is the final hop.
    if target_sha is not None:
        return target
    return None


# ── Fetch / diff / apply ─────────────────────────────────────────────


def fetch_updates() -> FetchResult:
    repo = _repo()
    _get_remote(repo)
    repo.git.fetch("origin", "--tags")

    if repo.is_dirty(untracked_files=True):
        return FetchResult(state="DIRTY")

    target = _get_target_ref(repo)
    if target is not None:
        sha = _resolve_ref_sha(repo, target)
        if sha is None:
            # The instance is pinned (git config openhost.target-ref) to a ref
            # that does not exist on the remote after fetching — a typo'd or
            # deleted branch/commit. Surface it instead of silently reporting
            # UP_TO_DATE, which would hide the operator's broken pin forever.
            raise RuntimeError(
                f"Pinned target ref '{target}' could not be resolved on the remote. "
                "Fix or clear the pin with 'set_remote' (a URL without an @ref clears it)."
            )
        if repo.head.commit.hexsha != sha:
            return FetchResult(state="BEHIND_REMOTE")
        return FetchResult(state="UP_TO_DATE")

    tags = _get_sorted_tags(repo)
    if not tags:
        return FetchResult(state="UP_TO_DATE")

    latest = tags[-1]
    current = _current_tag(repo) or _latest_ancestor_tag(repo)

    if current is None or _version_key(current) < _version_key(latest):
        return FetchResult(state="BEHIND_REMOTE")
    return FetchResult(state="UP_TO_DATE")


def show_diff() -> DiffResult:
    repo = _repo()
    current = _current_tag(repo) or _latest_ancestor_tag(repo) or repo.head.commit.hexsha[:8]

    target = _get_target_ref(repo)
    dest_label: str | None
    if target is not None:
        dest: str | None = _resolve_ref_sha(repo, target)
        dest_label = target
    else:
        tags = _get_sorted_tags(repo)
        dest = tags[-1] if tags else None
        dest_label = dest

    if dest is None:
        return DiffResult(commits=[], current_ref=current, remote_ref=None)

    commits = []
    for commit in repo.iter_commits(f"{current}..{dest}"):
        commits.append(
            DiffCommit(
                sha=commit.hexsha[:8],
                message=str(commit.message).strip().split("\n")[0],
            )
        )
    return DiffResult(commits=commits, current_ref=current, remote_ref=dest_label)


# STABILITY CONTRACT: this execs the checked-out tag's apply file by path
# and depends only on its exit code. Keep the path and that contract
# stable. Once a new tag is cut, the contract resets to that tag's code.
_APPLY_ENTRYPOINT = _PACKAGE_DIR / "apply_after_checkout.py"


def apply_update() -> NoReturn:
    repo = _repo()

    if repo.is_dirty(untracked_files=True):
        raise RuntimeError("Working tree has uncommitted changes. Stash or commit first.")

    repo.git.fetch("origin", "--tags")
    if not _get_sorted_tags(repo) and _get_target_ref(repo) is None:
        raise RuntimeError("No tags found on remote. Tag a release first.")

    # Take the first step (next tag, or the pinned target once tags are done);
    # apply_after_checkout tail-calls forward through the rest itself.
    step = _next_step(repo)
    if step is not None:
        logger.info(f"Checking out {step}...")
        repo.git.checkout(_resolve_ref_sha(repo, step) or step)
        repo.git.clean("-fd")

    # Hand off to the checked-out ref's apply code, replacing this process.
    # It walks any remaining steps and restarts openhost when done, so this
    # never returns; the migration log records what happened for the next boot.
    os.execv(sys.executable, [sys.executable, str(_APPLY_ENTRYPOINT)])


# ── Remote management ────────────────────────────────────────────────


def set_remote_url(url: str) -> RemoteInfo:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _KNOWN_SCHEMES:
        url = "https://" + url
        parsed = urllib.parse.urlparse(url)

    ref: str | None = None
    if "@" in parsed.path:
        base_path, ref = parsed.path.rsplit("@", 1)
        url = parsed._replace(path=base_path).geturl()

    repo = _repo()
    try:
        with _get_remote(repo).config_writer as cw:
            cw.set("url", url)
    except RuntimeError:
        repo.create_remote("origin", url)

    # An @ref pins the instance to that branch/commit (updates walk the tags but
    # end there instead of the latest tag); no @ref clears the pin. Persist the
    # pin only *after* the ref resolves and is checked out, so a bad @ref (typo,
    # deleted branch) raises without leaving a broken pin behind that would make
    # fetch_updates silently report UP_TO_DATE forever.
    if ref:
        _get_remote(repo).fetch()
        try:
            repo.refs[f"origin/{ref}"]
            repo.git.checkout("-fB", ref, f"origin/{ref}")
            repo.heads[ref].set_tracking_branch(repo.refs[f"origin/{ref}"])
        except IndexError:
            repo.git.checkout("-f", ref)
        repo.git.clean("-fd")

    _set_target_ref(repo, ref)

    return RemoteInfo(
        url=_strip_credentials(url),
        ref=ref or (tags[-1] if (tags := _get_sorted_tags(repo)) else "HEAD"),
        pinned=ref is not None,
    )


def get_remote_info() -> RemoteInfo:
    repo = _repo()
    remote = _get_remote(repo)
    url = _strip_credentials(remote.url) if remote.url else None
    target = _get_target_ref(repo)
    # When unpinned, still report the resolved current tag for display, but flag
    # it as not pinned so the dashboard doesn't round-trip it back into a pin.
    ref = target or _current_tag(repo) or _latest_ancestor_tag(repo) or repo.head.commit.hexsha[:8]
    return RemoteInfo(url=url, ref=ref, pinned=target is not None)
