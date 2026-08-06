"""Tests for ``apply_after_checkout``'s subprocess-based git walk helpers.

``apply_after_checkout.py`` reimplements the tag/target-ref walk with plain
``subprocess`` calls (rather than gitpython) because it must run standalone as
the re-exec entrypoint. It is where the production ``os.execv`` walk physically
loops, so its ``_next_step`` termination is tested directly here against real
throwaway git repos — mirroring ``test_update.py``'s coverage of the gitpython
twin so the two implementations cannot silently drift.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import openhost_system_agent.apply_after_checkout as aac

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


def _commit(path: Path, msg: str) -> None:
    (path / "VERSION").write_text(msg + "\n")
    _git(path, "add", "VERSION")
    _git(path, "commit", "-m", msg)


def _make_repo(path: Path, tags: list[str]) -> Path:
    path.mkdir(parents=True)
    _git(path, "-c", "init.defaultBranch=main", "init")
    for tag in tags:
        _commit(path, f"release {tag}")
        _git(path, "tag", tag)
    return path


def _clone_at(remote: Path, dest: Path, checkout: str | None = None) -> Path:
    _git(dest.parent, "clone", str(remote), dest.name)
    if checkout is not None:
        _git(dest, "checkout", checkout)
    return dest


def _branch(remote: Path, name: str, msg: str) -> None:
    _git(remote, "checkout", "-b", name)
    _commit(remote, msg)
    _git(remote, "checkout", "main")


def _branch_from(remote: Path, name: str, start: str, msg: str) -> None:
    _git(remote, "checkout", start)
    _git(remote, "checkout", "-b", name)
    _commit(remote, msg)
    _git(remote, "checkout", "main")


def _set_target(project: Path, ref: str) -> None:
    _git(project, "config", aac._TARGET_REF_CONFIG, ref)


def _walk(project: Path, *, max_hops: int = 50) -> list[str]:
    """Drive the real re-exec walk locally (minus migrations/pixi/execv):
    ``_next_step`` → checkout → repeat, until ``None``. Raises if it fails to
    terminate — the infinite-loop regression guard."""
    hops: list[str] = []
    for _ in range(max_hops):
        step = aac._next_step(str(project))
        if step is None:
            return hops
        hops.append(step)
        sha = aac._resolve_ref_sha(str(project), step) or step
        _git(project, "checkout", sha)
    raise AssertionError(f"walk did not terminate within {max_hops} hops: {hops}")


# ── Pure helpers ─────────────────────────────────────────────────────


def test_version_key_and_sorted_tags(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", ["v2.0.0", "v1.10.0", "v1.2.0"])
    _git(repo, "tag", "v1.0.0-rc1")  # pre-release: ignored
    _git(repo, "tag", "nightly")  # non-release: ignored
    assert aac._get_sorted_tags(str(repo)) == ["v1.2.0", "v1.10.0", "v2.0.0"]


def test_current_and_ancestor_tag(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", ["v1.0.0", "v1.1.0"])
    assert aac._current_tag(str(repo)) == "v1.1.0"
    assert aac._latest_ancestor_tag(str(repo)) == "v1.1.0"
    _commit(repo, "past the tag")
    assert aac._current_tag(str(repo)) is None
    assert aac._latest_ancestor_tag(str(repo)) == "v1.1.0"


def test_is_ancestor_helper(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "off old")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    v110 = aac._resolve_ref_sha(str(local), "v1.1.0")
    feat = aac._resolve_ref_sha(str(local), "feature")
    assert v110 is not None and feat is not None
    assert aac._is_ancestor(str(local), v110, feat) is False
    assert aac._is_ancestor(str(local), feat, feat) is True


# ── Walk termination across topologies ───────────────────────────────


def test_walk_unpinned_reaches_latest(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    assert _walk(local) == ["v1.1.0", "v1.2.0"]


def test_walk_unpinned_already_latest_noop(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    assert _walk(local) == []


def test_walk_pinned_branch_off_latest_terminates(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")  # off latest tag
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    _set_target(local, "feature")
    assert _walk(local) == ["v1.1.0", "feature"]


def test_walk_pinned_branch_off_older_tag_terminates(tmp_path: Path) -> None:
    # REGRESSION: the topology that made the production execv walk loop forever.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "off old tag")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    _set_target(local, "feature")
    assert _walk(local) == ["feature"]  # v1.1.0 skipped (not contained)


def test_walk_pinned_on_tip_terminal_with_newer_tag(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch_from(remote, "feature", "v1.0.0", "off old tag")
    local = _clone_at(remote, tmp_path / "local", checkout="feature")
    _set_target(local, "feature")
    assert aac._next_step(str(local)) is None
    assert _walk(local) == []


def test_walk_rollback_pin_terminates(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.2.0")
    _set_target(local, "v1.0.0")
    hops = _walk(local)
    assert hops in (["v1.0.0"], [])
    assert _git(local, "rev-parse", "HEAD") == aac._resolve_ref_sha(str(local), "v1.0.0")


def test_walk_pinned_multiple_intermediate_tags(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    _branch_from(remote, "feature", "v1.2.0", "off latest")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    _set_target(local, "feature")
    assert _walk(local) == ["v1.1.0", "v1.2.0", "feature"]


def test_walk_pinned_to_unresolvable_ref_raises(tmp_path: Path) -> None:
    # A pin to a ref that no longer resolves must raise, not silently walk to the
    # latest tag (which would abandon the operator's pin). Mirrors update.py.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    _set_target(local, "does-not-exist")
    assert aac._resolve_ref_sha(str(local), "does-not-exist") is None
    with pytest.raises(RuntimeError, match="could not be resolved"):
        aac._next_step(str(local))


def test_walk_pinned_to_unresolvable_ref_below_latest_tag_raises(tmp_path: Path) -> None:
    # REGRESSION: HEAD below the latest tag with an unresolvable pin. The old
    # walk would step forward to the next tag and jump to the latest release,
    # abandoning the pin. It must raise instead.
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v2.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    _set_target(local, "deleted-branch")
    with pytest.raises(RuntimeError, match="could not be resolved"):
        aac._next_step(str(local))


def test_walk_no_tags_with_pinned_target(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "-c", "init.defaultBranch=main", "init")
    _commit(remote, "base")
    _branch(remote, "feature", "feature work")
    local = _clone_at(remote, tmp_path / "local", checkout="main")
    _set_target(local, "feature")
    assert _walk(local) == ["feature"]


def test_ensure_repo_trusted_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Adds the repo to safe.directory once; a second call is a no-op (no dup).
    # Point HOME at an isolated dir so we write to a throwaway global gitconfig
    # rather than the developer's real one.
    repo = _make_repo(tmp_path / "repo", ["v1.0.0"])
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)

    aac._ensure_repo_trusted(str(repo))
    aac._ensure_repo_trusted(str(repo))

    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        capture_output=True,
        text=True,
    )
    assert result.stdout.count(str(repo)) == 1


def test_main_reclaims_host_ownership_before_migrations_and_install() -> None:
    # The failsafe must run FIRST: before migrations (a migration can run a
    # host-user pixi op, e.g. v0004's self-update, that would fail on a
    # root-owned tree and abort the update) and before the host-user
    # `pixi install`.
    order: list[str] = []

    def _install(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        order.append("install")
        return subprocess.CompletedProcess(args=[], returncode=0)

    with (
        patch.object(aac, "_ensure_repo_trusted"),
        patch.object(aac, "apply_system_migrations", side_effect=lambda: order.append("migrations")),
        patch.object(aac, "reclaim_host_ownership", side_effect=lambda: order.append("reclaim")),
        # The detached updater launch is a best-effort cosmetic side effect just
        # before the restart; stub it so the test neither spawns a real systemd
        # scope nor pollutes the subprocess-call ordering asserted below.
        patch.object(aac, "launch_updater", return_value=True),
        patch("openhost_system_agent.apply_after_checkout.subprocess.run", side_effect=_install) as mock_run,
        patch.object(aac, "_next_step", return_value=None),
    ):
        # No next step -> falls through to the systemctl restart, which our
        # mocked subprocess.run also records as an "install" entry; assert the
        # relative order of reclaim -> migrations -> first subprocess call.
        aac.main()

    assert order[:3] == ["reclaim", "migrations", "install"]
    # The first subprocess call is the host-user pixi install.
    first_call = mock_run.call_args_list[0]
    assert first_call.args[0] == ["sudo", "-u", "host", "-H", aac.PIXI_BIN, "install"]
    assert first_call.kwargs["timeout"] == 300
