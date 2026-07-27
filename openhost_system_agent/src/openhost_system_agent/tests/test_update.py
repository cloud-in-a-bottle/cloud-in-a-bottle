"""Tests for the tag-based update flow, run against real throwaway git repos.

Each test builds an actual git repo (and, where fetch matters, a local
file-based "origin" remote) in a tmp dir and points ``update._repo`` at it.
This exercises the real tag math, ``git describe``, checkout and ``git clean``
rather than mocking gitpython, while staying fast and offline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import git
import pytest

import openhost_system_agent.update as update_mod

# Fully isolate from the developer's global/system git config (signing, hooks,
# user identity) so tests are deterministic on any machine.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path: Path, msg: str = "wip") -> None:
    (path / "VERSION").write_text(msg + "\n")
    _git(path, "add", "VERSION")
    _git(path, "commit", "-m", msg)


def _commit_and_tag(path: Path, tag: str) -> None:
    _commit(path, f"release {tag}")
    _git(path, "tag", tag)


def _make_repo(path: Path, tags: list[str]) -> Path:
    """A standalone repo with one commit per tag (and no remote)."""
    path.mkdir(parents=True)
    _git(path, "-c", "init.defaultBranch=main", "init")
    for tag in tags:
        _commit_and_tag(path, tag)
    return path


def _clone_at(remote: Path, dest: Path, checkout: str | None = None) -> git.Repo:
    """Clone ``remote`` (so ``origin`` is set), optionally detaching at a tag."""
    _git(dest.parent, "clone", str(remote), dest.name)
    if checkout is not None:
        _git(dest, "checkout", checkout)
    return git.Repo(dest)


def _stub_execv(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("openhost_system_agent.update.os.execv", lambda *a: calls.append(a))
    return calls


# ── Pure tag helpers ─────────────────────────────────────────────────


def test_version_key_orders_release_tags() -> None:
    vk = update_mod._version_key
    assert vk("v1") < vk("v1.0") < vk("v1.0.0") < vk("v2") < vk("v10")
    assert vk("v1.2.0") < vk("v1.10.0")  # numeric, not lexicographic


def test_get_sorted_tags_filters_non_release_and_sorts(tmp_path: Path) -> None:
    repo_dir = _make_repo(tmp_path / "repo", ["v2.0.0", "v1.10.0", "v1.2.0"])
    _git(repo_dir, "tag", "v1.0.0-rc1")  # pre-release: ignored
    _git(repo_dir, "tag", "nightly")  # non-release: ignored
    repo = git.Repo(repo_dir)

    assert update_mod._get_sorted_tags(repo) == ["v1.2.0", "v1.10.0", "v2.0.0"]


def test_current_and_ancestor_tag(tmp_path: Path) -> None:
    repo_dir = _make_repo(tmp_path / "repo", ["v1.0.0", "v1.1.0"])
    repo = git.Repo(repo_dir)

    assert update_mod._current_tag(repo) == "v1.1.0"
    assert update_mod._latest_ancestor_tag(repo) == "v1.1.0"

    # Move past the tag: no longer *exactly* on it, but it's still the ancestor.
    _commit(repo_dir, "past the tag")
    assert update_mod._current_tag(repo) is None
    assert update_mod._latest_ancestor_tag(repo) == "v1.1.0"


# ── fetch_updates ────────────────────────────────────────────────────


def test_fetch_updates_behind_after_remote_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    _commit_and_tag(remote, "v1.1.0")  # new release the local doesn't have yet
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    # fetch_updates fetches origin --tags, so it discovers v1.1.0.
    assert update_mod.fetch_updates().state == "BEHIND_REMOTE"


def test_fetch_updates_up_to_date_on_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "UP_TO_DATE"


def test_fetch_updates_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    (tmp_path / "local" / "untracked").write_text("x")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "DIRTY"


def test_fetch_updates_without_origin_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_dir = _make_repo(tmp_path / "repo", ["v1.0.0"])  # no remote
    monkeypatch.setattr(update_mod, "_repo", lambda: git.Repo(repo_dir))

    with pytest.raises(RuntimeError, match="origin"):
        update_mod.fetch_updates()


# ── apply_update ─────────────────────────────────────────────────────


def test_apply_update_steps_onto_immediate_next_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v2.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    # Stepping-stone walk: we move to v1.1.0, NOT straight to the latest v2.0.0.
    assert update_mod._current_tag(local) == "v1.1.0"
    assert len(calls) == 1  # handed off to the apply walk exactly once


def test_apply_update_on_latest_hands_off_without_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    head_before = local.head.commit.hexsha
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    assert local.head.commit.hexsha == head_before  # no checkout happened
    assert len(calls) == 1


def test_apply_update_with_no_ancestor_tag_starts_at_first_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HEAD sits on a commit that predates every tag, so current is None.
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "-c", "init.defaultBranch=main", "init")
    _commit(remote, "base")  # untagged root commit
    _commit_and_tag(remote, "v1.0.0")  # tag is a descendant, not an ancestor
    local = _clone_at(remote, tmp_path / "local", checkout="HEAD~1")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod._latest_ancestor_tag(local) is None  # precondition
    update_mod.apply_update()

    assert update_mod._current_tag(local) == "v1.0.0"  # started the walk at tags[0]
    assert len(calls) == 1


def test_apply_update_dirty_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    (tmp_path / "local" / "untracked").write_text("x")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(RuntimeError, match="uncommitted"):
        update_mod.apply_update()
    assert calls == []  # never handed off


def test_apply_update_without_tags_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "-c", "init.defaultBranch=main", "init")
    _commit(remote, "only commit, no tags")
    local = _clone_at(remote, tmp_path / "local")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(RuntimeError, match="No tags"):
        update_mod.apply_update()
    assert calls == []


# ── show_diff / get_remote_info ──────────────────────────────────────


def test_show_diff_lists_commits_between_current_and_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    diff = update_mod.show_diff()

    assert diff.current_ref == "v1.0.0"
    assert diff.remote_ref == "v1.2.0"
    assert len(diff.commits) == 2  # the v1.1.0 and v1.2.0 commits


def test_get_remote_info_reports_current_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    info = update_mod.get_remote_info()

    assert info.ref == "v1.0.0"
    assert info.url is not None and "remote" in info.url
    # Unpinned: ref is informational only and must not be flagged as a pin, so
    # the dashboard won't round-trip it back into a silent pin.
    assert info.pinned is False


# ── Pinned target ref (run from a branch/commit) ─────────────────────


def _branch(remote: Path, name: str, msg: str) -> None:
    """Create a feature branch one commit ahead of the current HEAD."""
    _git(remote, "checkout", "-b", name)
    _commit(remote, msg)
    _git(remote, "checkout", "main")


def test_next_step_walks_tags_first_then_pinned_target(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, "feature")

    # Behind in tags: still step through the tag stones first.
    assert update_mod._next_step(repo) == "v1.1.0"

    # On the latest tag: the pinned branch is the final hop.
    _git(tmp_path / "local", "checkout", "v1.1.0")
    assert update_mod._next_step(repo) == "feature"

    # On the branch tip: nothing left to do.
    tip = update_mod._resolve_ref_sha(repo, "feature")
    assert tip is not None
    _git(tmp_path / "local", "checkout", tip)
    assert update_mod._next_step(repo) is None


def test_apply_update_pinned_still_steps_through_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    # First step is the next tag, not a jump straight to the pinned branch.
    assert update_mod._current_tag(local) == "v1.1.0"
    assert len(calls) == 1


def test_fetch_updates_pinned_behind_when_branch_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    _git(remote, "checkout", "feature")
    _commit(remote, "f2")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "BEHIND_REMOTE"


def test_fetch_updates_pinned_up_to_date_on_branch_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="feature")
    update_mod._set_target_ref(local, "feature")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "UP_TO_DATE"


def test_set_remote_url_pins_and_clears_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.set_remote_url(f"file://{remote}@feature")
    assert update_mod._get_target_ref(local) == "feature"

    update_mod.set_remote_url(f"file://{remote}")
    assert update_mod._get_target_ref(local) is None


def test_fetch_updates_pinned_unresolvable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pin to a ref that cannot be resolved after fetch (typo'd or deleted
    # branch) must surface as an error, not silently report UP_TO_DATE and hide
    # the broken pin forever.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "no-such-branch")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(RuntimeError, match="could not be resolved"):
        update_mod.fetch_updates()


def test_get_remote_info_reports_pinned_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    info = update_mod.get_remote_info()
    assert info.ref == "feature"
    assert info.pinned is True  # actually pinned -> dashboard may reconstruct @feature


def test_get_remote_info_unpinned_not_flagged_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # REGRESSION: an unpinned instance reports the resolved current tag as ref
    # (for display) but must set pinned=False. Otherwise the dashboard round-
    # trips url@ref into set_remote and silently pins the host to the current
    # tag, freezing it out of future auto-updates.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod._get_target_ref(local) is None  # unpinned
    info = update_mod.get_remote_info()
    assert info.ref == "v1.1.0"  # still shown for information
    assert info.pinned is False  # but not a pin


def test_set_remote_url_reports_pinned_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    pinned = update_mod.set_remote_url(f"file://{remote}@feature")
    assert pinned.pinned is True and pinned.ref == "feature"

    unpinned = update_mod.set_remote_url(f"file://{remote}")
    assert unpinned.pinned is False


# ── Extended: _next_step termination + stepping-stone topologies ─────
#
# These exercise the walk against every important topology, especially the
# ones the original implementation looped forever on: a pinned ref that does
# not contain the newest release tag (branch cut from an older tag, or a
# rollback pin to an older commit). The invariant under test: from any starting
# point, repeatedly applying ``_next_step`` (checking out its result each time)
# must reach ``None`` in a bounded number of hops, and land on the intended
# destination.


def _branch_from(remote: Path, name: str, start: str, msg: str) -> None:
    """Create ``name`` off ``start`` (a tag/branch/sha), one commit ahead."""
    _git(remote, "checkout", start)
    _git(remote, "checkout", "-b", name)
    _commit(remote, msg)
    _git(remote, "checkout", "main")


def _walk(repo: git.Repo, *, max_hops: int = 50) -> list[str]:
    """Drive the real apply walk locally: repeatedly ask ``_next_step`` for the
    next ref, check out its resolved commit, and record it, until ``None``.

    Mirrors ``apply_after_checkout.main``'s checkout loop (minus migrations /
    pixi / execv). Raises if it fails to terminate within ``max_hops`` — that is
    exactly the infinite-loop regression we are guarding against."""
    path = Path(repo.working_dir)
    hops: list[str] = []
    for _ in range(max_hops):
        step = update_mod._next_step(repo)
        if step is None:
            return hops
        hops.append(step)
        sha = update_mod._resolve_ref_sha(repo, step) or step
        _git(path, "checkout", sha)
    raise AssertionError(f"walk did not terminate within {max_hops} hops: {hops}")


def test_walk_unpinned_reaches_latest_tag(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")

    hops = _walk(repo)

    assert hops == ["v1.1.0", "v1.2.0"]
    assert update_mod._current_tag(repo) == "v1.2.0"


def test_walk_unpinned_already_latest_is_noop(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")

    assert _walk(repo) == []


def test_walk_pinned_branch_off_latest_tag_terminates(tmp_path: Path) -> None:
    # feature descends from the latest tag: walk tags, then hop to feature.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")  # off main == v1.1.0
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, "feature")

    hops = _walk(repo)

    assert hops == ["v1.1.0", "feature"]
    tip = update_mod._resolve_ref_sha(repo, "feature")
    assert repo.head.commit.hexsha == tip


def test_walk_pinned_branch_off_older_tag_terminates(tmp_path: Path) -> None:
    # REGRESSION: feature cut from v1.0.0 while v1.1.0 exists on main and is NOT
    # an ancestor of feature. The original _next_step ping-ponged v1.1.0<->feature
    # forever. The walk must terminate and skip the un-contained tag.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "feature off old tag")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, "feature")

    hops = _walk(repo)

    # v1.1.0 is not contained by feature, so it is skipped entirely.
    assert hops == ["feature"]
    tip = update_mod._resolve_ref_sha(repo, "feature")
    assert repo.head.commit.hexsha == tip


def test_walk_pinned_on_branch_tip_is_terminal_even_with_newer_tag(tmp_path: Path) -> None:
    # REGRESSION: sitting exactly on the pinned tip while a newer, un-contained
    # tag exists must be terminal (previously it stepped onto the newer tag).
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "feature off old tag")
    repo = _clone_at(remote, tmp_path / "local", checkout="feature")
    update_mod._set_target_ref(repo, "feature")

    assert update_mod._next_step(repo) is None
    assert _walk(repo) == []


def test_walk_rollback_pin_to_older_commit_terminates(tmp_path: Path) -> None:
    # REGRESSION: pin to an OLDER tag/commit than current HEAD (a rollback).
    # The walk must not chase forward to a newer tag; it should be a no-op /
    # single hop that terminates rather than looping.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.2.0")
    update_mod._set_target_ref(repo, "v1.0.0")

    hops = _walk(repo)

    # Only v1.0.0 is an ancestor of the target sha; no forward chase to v1.1/2.
    assert hops in (["v1.0.0"], [])
    assert repo.head.commit.hexsha == update_mod._resolve_ref_sha(repo, "v1.0.0")


def test_walk_pinned_multiple_intermediate_tags_are_stepped(tmp_path: Path) -> None:
    # feature off v1.2.0 while v1.0/1.1/1.2 all exist: step every contained tag
    # after current, then hop to feature.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    _branch_from(remote, "feature", "v1.2.0", "feature off latest")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, "feature")

    hops = _walk(repo)

    assert hops == ["v1.1.0", "v1.2.0", "feature"]


def test_walk_pinned_partway_up_tags(tmp_path: Path) -> None:
    # Start already on v1.1.0, pinned to feature off v1.2.0: only v1.2.0 then hop.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    _branch_from(remote, "feature", "v1.2.0", "feature off latest")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    update_mod._set_target_ref(repo, "feature")

    assert _walk(repo) == ["v1.2.0", "feature"]


def test_walk_pinned_to_sha_directly(tmp_path: Path) -> None:
    # Pin to a raw commit sha (not a branch name) that is a descendant of latest.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _git(remote, "checkout", "main")
    _commit(remote, "past latest")
    target_sha = _git(remote, "rev-parse", "HEAD")
    _git(remote, "checkout", "main")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, target_sha)

    hops = _walk(repo)

    assert hops[-1] == target_sha
    assert repo.head.commit.hexsha == target_sha


def test_next_step_no_tags_no_target_is_none(tmp_path: Path) -> None:
    # A repo with commits but no release tags and no pin: nothing to do.
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "-c", "init.defaultBranch=main", "init")
    _commit(path, "only commit")
    repo = git.Repo(path)

    assert update_mod._next_step(repo) is None


def test_next_step_no_tags_with_pinned_target(tmp_path: Path) -> None:
    # No release tags at all, but a branch is pinned: hop straight to it.
    path = tmp_path / "remote"
    path.mkdir()
    _git(path, "-c", "init.defaultBranch=main", "init")
    _commit(path, "base")
    _branch(path, "feature", "feature work")
    repo = _clone_at(path, tmp_path / "local", checkout="main")
    update_mod._set_target_ref(repo, "feature")

    assert _walk(repo) == ["feature"]


def test_next_step_pinned_to_unresolvable_ref_raises(tmp_path: Path) -> None:
    # A pin to a ref that cannot be resolved (typo, or a branch/commit deleted
    # on the remote after it was pinned) must raise, not silently walk. Here HEAD
    # is at the latest tag so the OLD code happened to return None; the fix
    # surfaces the broken pin consistently with fetch_updates (see ca961ab).
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    update_mod._set_target_ref(repo, "does-not-exist")

    assert update_mod._resolve_ref_sha(repo, "does-not-exist") is None
    with pytest.raises(RuntimeError, match="could not be resolved"):
        update_mod._next_step(repo)


def test_next_step_pinned_to_unresolvable_ref_below_latest_tag_raises(tmp_path: Path) -> None:
    # REGRESSION: HEAD is BELOW the latest tag while pinned to an unresolvable
    # ref. The old _next_step returned the next tag and would have walked the
    # host all the way to the latest release, silently abandoning the operator's
    # pin. It must raise instead of stepping.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v2.0.0"])
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, "deleted-branch")

    with pytest.raises(RuntimeError, match="could not be resolved"):
        update_mod._next_step(repo)


def test_apply_update_pinned_to_unresolvable_ref_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # apply_update must surface a broken pin rather than execing into a walk that
    # jumps the host to the latest tag, abandoning the pin.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "deleted-branch")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(RuntimeError, match="could not be resolved"):
        update_mod.apply_update()
    assert calls == []  # never handed off to the walk


def test_is_ancestor_helper(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "off old")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")

    v100 = update_mod._resolve_ref_sha(repo, "v1.0.0")
    v110 = update_mod._resolve_ref_sha(repo, "v1.1.0")
    feat = update_mod._resolve_ref_sha(repo, "feature")
    assert v100 is not None and v110 is not None and feat is not None

    assert update_mod._is_ancestor(repo, v100, v110) is True
    assert update_mod._is_ancestor(repo, v100, feat) is True
    # v1.1.0 is NOT contained by feature (feature branched off v1.0.0).
    assert update_mod._is_ancestor(repo, v110, feat) is False
    # A commit is its own ancestor (equal case) — keeps the terminal check sound.
    assert update_mod._is_ancestor(repo, feat, feat) is True


def test_apply_update_pinned_off_old_tag_takes_bounded_first_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # REGRESSION at the apply_update entrypoint: with a pin that doesn't contain
    # the newest tag, the first step must be the target (or a contained tag),
    # never the un-contained newest tag, and it must execv exactly once.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "off old tag")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    assert len(calls) == 1
    # HEAD advanced to the feature tip, not the un-contained v1.1.0.
    assert local.head.commit.hexsha == update_mod._resolve_ref_sha(local, "feature")


def test_apply_update_on_pinned_tip_is_noop_walk_then_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Already on the pinned tip (with a newer un-contained tag present): the
    # first hop is terminal, so apply_update execs apply_after_checkout directly
    # (which will restart) without stepping onto any tag.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "off old tag")
    local = _clone_at(remote, tmp_path / "local", checkout="feature")
    update_mod._set_target_ref(local, "feature")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)
    before = local.head.commit.hexsha

    update_mod.apply_update()

    assert len(calls) == 1
    assert local.head.commit.hexsha == before  # no checkout happened


def test_show_diff_pinned_lists_commits_to_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    diff = update_mod.show_diff()

    assert diff.remote_ref == "feature"
    assert len(diff.commits) >= 1


def test_fetch_updates_rollback_pin_reports_behind_then_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rollback pin to an older tag reports BEHIND_REMOTE until HEAD matches it,
    # and never loops.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    update_mod._set_target_ref(local, "v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "BEHIND_REMOTE"

    v100_sha = update_mod._resolve_ref_sha(local, "v1.0.0")
    assert v100_sha is not None
    _git(tmp_path / "local", "checkout", v100_sha)
    assert update_mod.fetch_updates().state == "UP_TO_DATE"


def test_set_remote_url_roundtrip_pin_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.set_remote_url(f"file://{remote}@feature")
    update_mod.set_remote_url(f"file://{remote}@feature")  # re-pin same ref
    assert update_mod._get_target_ref(local) == "feature"

    update_mod.set_remote_url(f"file://{remote}")
    update_mod.set_remote_url(f"file://{remote}")  # re-clear when already clear
    assert update_mod._get_target_ref(local) is None


def test_set_remote_url_bad_ref_does_not_persist_broken_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pinning to a ref that doesn't exist must raise AND leave no target-ref
    # persisted. Otherwise fetch_updates would silently report UP_TO_DATE for an
    # unresolvable pin, hiding the misconfiguration and freezing the host.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(git.GitCommandError):
        update_mod.set_remote_url(f"file://{remote}@nonexistent-branch")

    # No broken pin left behind.
    assert update_mod._get_target_ref(local) is None
    # And with no pin persisted, fetch_updates uses normal tag logic (on latest).
    assert update_mod.fetch_updates().state == "UP_TO_DATE"


def test_set_remote_url_bad_re_pin_keeps_prior_working_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A working pin already exists; a subsequent re-pin to a bad ref fails but
    # must not clobber the good pin.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.set_remote_url(f"file://{remote}@feature")
    assert update_mod._get_target_ref(local) == "feature"

    with pytest.raises(git.GitCommandError):
        update_mod.set_remote_url(f"file://{remote}@nonexistent-branch")

    # The good pin survives the failed re-pin.
    assert update_mod._get_target_ref(local) == "feature"
