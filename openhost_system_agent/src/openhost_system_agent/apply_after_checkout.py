"""Apply entrypoint: run this checkout's migrations, install deps, then
tail-call into the next ref; start openhost once on the destination.

openhost is already stopped when this runs (update.apply_update takes it down
before the first checkout) and this runs as its own systemd unit, so migrations
do not race a live router -- though the system is not fully quiesced, see
update.apply_update.

At each step: migrations → pixi install → checkout next ref → os.execv self.
Using execv (not a child subprocess) keeps the walk a single process no
matter how many steps the host is behind, and each step still runs that
ref's own code.

The destination is the latest release tag, unless a target ref is pinned
(git config openhost.target-ref, e.g. a feature branch) — then the tags are
still walked as stepping stones and the pinned ref is the final hop. With no
pin and the owner opted in (git config openhost.prereleases), the newest
release candidate is the final hop when it is newer than the releases.

Migrations run before `pixi install`, so a migration that upgrades the
toolchain (e.g. pixi) takes effect before deps are installed.

There is no structured output contract: success starts openhost and the
migration log records what happened. Failure is a non-zero exit with the error on
stderr — and the apply unit's ExecStopPost starts openhost either way.

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

from openhost_system_agent.detach import start_openhost
from openhost_system_agent.migrations.runner import apply_system_migrations
from openhost_system_agent.reclaim import reclaim_host_ownership
from openhost_system_agent.updater import progress

PIXI_BIN = "/home/host/.pixi/bin/pixi"

# A release tag is "v" followed by dot-separated integers (v1, v1.2, v1.2.3).
# A release candidate adds "-rc.N" (v1.2.0-rc.1); it is never a stepping stone,
# only the optional last hop. Any other v-prefixed tag is ignored so version
# parsing can't crash on it.
_RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")
_RC_TAG = re.compile(r"v\d+(?:\.\d+)*-rc\.\d+")


# When set, updates end on this ref (a branch tip or commit) instead of the
# latest tag — kept in sync with update.py's _TARGET_REF_CONFIG.
_TARGET_REF_CONFIG = "openhost.target-ref"

# When true (and no target ref is pinned), the walk ends on the newest release
# candidate if it is newer than the releases — kept in sync with update.py's
# _PRERELEASES_CONFIG.
_PRERELEASES_CONFIG = "openhost.prereleases"


def _git(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=60)


def _ensure_repo_trusted(project: str) -> None:
    # This runs as root on a host-owned repo; trust it so git doesn't refuse
    # with "detected dubious ownership".
    existing = _git(project, "config", "--global", "--get-all", "safe.directory").stdout.split("\n")
    if project not in existing:
        _git(project, "config", "--global", "--add", "safe.directory", project)


def _version_key(tag_name: str) -> tuple[tuple[int, ...], int, int]:
    version, _, rc = tag_name.lstrip("v").partition("-rc.")
    # A release sorts after its own candidates: v1.2.0-rc.1 < v1.2.0-rc.2 < v1.2.0.
    return tuple(int(x) for x in version.split(".")), 0 if rc else 1, int(rc or 0)


def _is_version_tag(name: str) -> bool:
    return bool(_RELEASE_TAG.fullmatch(name) or _RC_TAG.fullmatch(name))


def _tag_list(project: str, *args: str) -> list[str]:
    result = _git(project, "tag", *args)
    return result.stdout.split() if result.returncode == 0 else []


def _get_sorted_tags(project: str) -> list[str]:
    return sorted((t for t in _tag_list(project, "-l", "v*") if _RELEASE_TAG.fullmatch(t)), key=_version_key)


def _newest_candidate(project: str) -> str | None:
    return max((t for t in _tag_list(project, "-l", "v*") if _RC_TAG.fullmatch(t)), key=_version_key, default=None)


def _current_tag(project: str) -> str | None:
    """The highest release or candidate tag on HEAD itself. Not ``git describe --exact-match``, which may name a
    candidate sharing its release's commit. Kept in sync with update._current_tag."""
    return max(
        (t for t in _tag_list(project, "--points-at", "HEAD") if _is_version_tag(t)), key=_version_key, default=None
    )


def _latest_ancestor_tag(project: str) -> str | None:
    """The highest release or candidate tag in HEAD's history (HEAD included). Candidates count so a host on one is
    never walked back to an older line's release. Not ``git describe``, which returns only the nearest tag of any
    name, so an unrecognized tag nearer to HEAD would hide every release behind it.

    Kept in sync with update._latest_ancestor_tag.
    """
    return max(
        (t for t in _tag_list(project, "--merged", "HEAD") if _is_version_tag(t)), key=_version_key, default=None
    )


def _target_ref(project: str) -> str | None:
    result = _git(project, "config", "--get", _TARGET_REF_CONFIG)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _prereleases(project: str) -> bool:
    result = _git(project, "config", "--get", "--bool", _PRERELEASES_CONFIG)
    return result.returncode == 0 and result.stdout.strip() == "true"


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
    pinned target when set, otherwise the latest release tag, or the newest
    release candidate when the owner opted in and it is newer.

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

    # Releases exhausted on the tags channel: an opted-in host jumps straight to the newest candidate.
    candidate = _newest_candidate(project) if _prereleases(project) else None
    if candidate is not None and (base is None or _version_key(candidate) > _version_key(base)):
        return candidate
    return None


def _apply(project: str) -> None:
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

    # Install as the unprivileged 'host' user, not root. The Cloud in a Bottle service
    # runs as host via `pixi run`, and pixi tracks its PyPI sync per-user; a
    # root install leaves root-owned files in the env that the host service
    # then can't update, so its `pixi run` fails with EACCES.
    progress.record("install", "Installing dependencies\u2026")
    result = subprocess.run(["sudo", "-u", "host", "-H", PIXI_BIN, "install"], cwd=project, timeout=300)
    if result.returncode != 0:
        progress.record(progress.Phase.FAILED, f"Dependency install failed (exit {result.returncode}).")
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

    # NOT terminal (see Phase.RESTARTING): only the freshly booted compute_space
    # appends "done", so the page can't finish against the about-to-die instance.
    progress.record(progress.Phase.RESTARTING, "Update complete. Starting\u2026")

    start_openhost()


def main() -> None:
    # src/openhost_system_agent/apply_after_checkout.py → repo root is four up.
    project = str(Path(__file__).resolve().parents[3])
    _ensure_repo_trusted(project)

    # Any unhandled failure must leave a terminal "failed" entry, or the
    # /updating page (which only stops on done/failed) would spin forever with
    # no explanation. SystemExit(1) paths above record their own message.
    try:
        _apply(project)
    except SystemExit:
        raise
    except BaseException as e:
        progress.record(progress.Phase.FAILED, f"Update failed: {e}")
        raise


if __name__ == "__main__":
    main()
