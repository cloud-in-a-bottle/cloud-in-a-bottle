"""Apply entrypoint: run this checkout's migrations, install deps, then
tail-call into the next ref; restart openhost once on the destination.

At each step: migrations → pixi install → checkout next ref → os.execv self.
Using execv (not a child subprocess) keeps the walk a single process no
matter how many steps the host is behind, and each step still runs that
ref's own code.

The destination is the latest release tag, unless a target ref is pinned
(git config openhost.target-ref, e.g. a feature branch) — then the tags are
still walked as stepping stones and the pinned ref is the final hop.

Migrations run before `pixi install`, so a migration that upgrades the
toolchain (e.g. pixi) takes effect before deps are installed.

There is no structured output contract: success restarts openhost (which
may kill this process — see main()), and the migration log records what
happened for the freshly-started compute_space to read. Failure is a
non-zero exit with the error on stderr.

STABILITY CONTRACT: the prior tag's update.py execs this file by path and
depends only on its exit code. Keep the path and that contract stable. Once
a new tag is cut, the contract resets to that tag's update.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from openhost_system_agent.migrations.runner import apply_system_migrations
from openhost_system_agent.reclaim import reclaim_host_ownership
from openhost_system_agent.updater import progress
from openhost_system_agent.updater.launcher import launch_updater

PIXI_BIN = "/home/host/.pixi/bin/pixi"

# A release tag is "v" followed by dot-separated integers (v1, v1.2, v1.2.3).
# Other v-prefixed tags (e.g. v1.2.0-rc1) are ignored so version parsing
# can't crash on them.
_RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")


# When set, updates end on this ref (a branch tip or commit) instead of the
# latest tag — kept in sync with update.py's _TARGET_REF_CONFIG.
_TARGET_REF_CONFIG = "openhost.target-ref"


def _git(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=60)


def _ensure_repo_trusted(project: str) -> None:
    # This runs as root on a host-owned repo; trust it so git doesn't refuse
    # with "detected dubious ownership".
    existing = _git(project, "config", "--global", "--get-all", "safe.directory").stdout.split("\n")
    if project not in existing:
        _git(project, "config", "--global", "--add", "safe.directory", project)


def _version_key(tag_name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag_name.lstrip("v").split("."))


def _get_sorted_tags(project: str) -> list[str]:
    tags = [t for t in _git(project, "tag", "-l", "v*").stdout.strip().split("\n") if _RELEASE_TAG.fullmatch(t)]
    return sorted(tags, key=_version_key)


def _current_tag(project: str) -> str | None:
    result = _git(project, "describe", "--tags", "--exact-match", "HEAD")
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and _RELEASE_TAG.fullmatch(tag) else None


def _latest_ancestor_tag(project: str) -> str | None:
    result = _git(project, "describe", "--tags", "--abbrev=0", "HEAD")
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and _RELEASE_TAG.fullmatch(tag) else None


def _target_ref(project: str) -> str | None:
    result = _git(project, "config", "--get", _TARGET_REF_CONFIG)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _resolve_ref_sha(project: str, ref: str) -> str | None:
    """Resolve a ref to a commit sha, preferring the fetched remote branch tip."""
    for candidate in (f"origin/{ref}", ref):
        result = _git(project, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _is_ancestor(project: str, ancestor: str, descendant: str) -> bool:
    """True if ``ancestor`` is an ancestor of (or equal to) ``descendant``."""
    return _git(project, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _next_step(project: str) -> str | None:
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
    target = _target_ref(project)
    target_sha = _resolve_ref_sha(project, target) if target is not None else None

    # Pinned to a ref that no longer resolves (typo, or a branch/commit deleted
    # on the remote after it was pinned). Stop instead of silently walking to the
    # latest tag, which would abandon the operator's pin and jump the host onto a
    # release they explicitly opted out of. Mirrors update.fetch_updates.
    if target is not None and target_sha is None:
        raise RuntimeError(
            f"Pinned target ref '{target}' could not be resolved on the remote. "
            "Fix or clear the pin with 'set_remote' (a URL without an @ref clears it)."
        )

    # Terminal: already sitting on the pinned destination.
    if target_sha is not None and _git(project, "rev-parse", "HEAD").stdout.strip() == target_sha:
        return None

    base = _current_tag(project) or _latest_ancestor_tag(project)
    later = [t for t in _get_sorted_tags(project) if base is None or _version_key(t) > _version_key(base)]
    if target_sha is not None:
        # Only step through tags the pinned target actually contains, so the
        # walk stays monotonic toward the target instead of ping-ponging.
        later = [t for t in later if _is_ancestor(project, t, target_sha)]
    if later:
        return later[0]

    # Tags exhausted (or none applicable): the pinned target is the final hop.
    if target_sha is not None:
        return target
    return None


def main() -> None:
    # src/openhost_system_agent/apply_after_checkout.py → repo root is four up.
    project = str(Path(__file__).resolve().parents[3])
    _ensure_repo_trusted(project)

    # Failsafe: hand the pixi trees back to the host user FIRST, before anything
    # else touches them. This must precede migrations because a migration can
    # run a pixi operation as the host user (e.g. v0004's `pixi self-update`);
    # if an older root-run left `/home/host/.pixi` root-owned, that host-user
    # step would fail with EACCES and abort the whole update — never reaching a
    # later reclaim — leaving the host bricked. It also precedes the host-user
    # `pixi install` below and heals root-owned residue left by prior updates.
    # Runs as root (this whole apply is root via sudo).
    reclaim_host_ownership()

    # Migrations run before install so a toolchain upgrade (e.g. pixi) takes
    # effect before deps are installed for this checkout.
    progress.record("migrate", "Applying system migrations\u2026")
    apply_system_migrations()

    # Install as the unprivileged 'host' user, not root. The openhost service
    # runs as host via `pixi run`, and pixi tracks its PyPI sync per-user; a
    # root install leaves root-owned files in the env that the host service
    # then can't update, so its `pixi run` fails with EACCES.
    progress.record("install", "Installing dependencies\u2026")
    result = subprocess.run(["sudo", "-u", "host", "-H", PIXI_BIN, "install"], cwd=project, timeout=300)
    if result.returncode != 0:
        progress.record(progress.PHASE_FAILED, f"Dependency install failed (exit {result.returncode}).")
        print(f"pixi install failed (exit {result.returncode})", file=sys.stderr)
        sys.exit(1)

    nxt = _next_step(project)
    if nxt:
        # Check out the resolved commit (a branch tip becomes detached HEAD).
        progress.record("checkout", f"Checking out {nxt}\u2026", ref=nxt)
        subprocess.run(["git", "checkout", _resolve_ref_sha(project, nxt) or nxt], cwd=project, check=True, timeout=60)
        subprocess.run(["git", "clean", "-fd"], cwd=project, check=True, timeout=60)
        # Tail-call into the next ref's code, replacing this process so the
        # walk stays a single process regardless of how many steps we're behind.
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])

    # Destination reached. Record "done" BEFORE the restart: the restart kills
    # this process, so anything after systemctl may never run. The updater
    # server tails the log and reloads the browser onto the fresh dashboard.
    progress.record(progress.PHASE_DONE, "Update complete. Restarting\u2026")

    # Launch the detached downtime server just before the restart, so it is
    # ready to bind 80/443 the instant Caddy releases them. It runs in its own
    # systemd scope and therefore survives the cgroup-wide SIGTERM below.
    # Best-effort: launch_updater never raises.
    launch_updater()

    # On the destination: restart openhost so the new code takes over. When the
    # update was triggered from the dashboard this process shares openhost's
    # cgroup, so the restart's SIGTERM kills us mid-call — that's fine, systemd
    # still completes the restart and the new compute_space reads the log.
    result = subprocess.run(["systemctl", "restart", "openhost"], timeout=120)
    if result.returncode != 0:
        print(f"failed to restart openhost (exit {result.returncode})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
