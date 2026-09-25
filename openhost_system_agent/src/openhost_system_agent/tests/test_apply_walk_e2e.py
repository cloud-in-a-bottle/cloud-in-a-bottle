"""Functional end-to-end container tests for the tag-walk update framework.

These drive the *real* ``openhost_system_agent update`` entrypoints (apply /
fetch / show_diff) against real git repos + file-based bare origins built
inside an Ubuntu+systemd container, exercising the phased-update control flow:

  * multi-tag walk in a single ``os.execv`` chain (v1 → v2 → v3),
  * fetch state reporting (UP_TO_DATE / BEHIND_REMOTE),
  * dirty-tree rejection,
  * no-tags rejection,
  * a pinned target ref (``git config openhost.target-ref``) as the final hop
    after the tags are walked,
  * ``show_diff`` pending-commit listing,
  * idempotent re-apply on an already-latest host,
  * migrations alone installing and enabling the openhost.service unit,
    independent of any tag walk,
  * offline crash recovery with ownership repair,
  * automatic recovery when dependency installation fails during an update.

DESIGN DECISION — migrations run once up front, then re-run as no-ops.
``setup_class`` runs the real migrations once so the baseline (v2) installs and
enables the ``openhost.service`` systemd unit — the apply-walk's final step does
``systemctl restart openhost`` and needs that unit to exist. Running them also
advances the migration log to the registry's highest version, so each
per-test walk re-runs migrations as fast no-ops (the runner skips every
migration whose version is ``<= current``) — the walk then only does
``pixi install`` + git checkout at each step, keeping the tests fast and
deterministic no matter how many tags they step through. The *migration +
pixi-upgrade* path is covered end-to-end by ``TestApplyUpdateWalk`` in
test_migration_container.py, so these tests focus on the tag-walk / fetch /
apply / show_diff / target-ref control flow.

Because migrations are skipped, the walk does not depend on apt/iptables state
and can safely step through several tags in one invocation. Every scenario
shares ONE container (built + migrated once) to save container-lifecycle
overhead: the edge cases each rebuild the host git repo from scratch
(``rm -rf .git``), so they are order-independent among themselves and can run
after the walk scenarios in the same container.

Requires podman and the --run-containers flag (see root conftest.py); without
it every class here is skipped by the ``requires_containers`` marker.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest

# Bootstrapping the migration log to the registry's highest version makes every
# migration a skipped no-op during a pure tag walk. Derived from the registry so
# it can't drift when a migration is added.
from openhost_system_agent.migrations.registry import REGISTRY as _REGISTRY
from openhost_system_agent.migrations.registry import latest_registry_version as _latest_registry_version

# Reuse the container-test helper toolkit from the sibling container test module.
# Importing keeps a single source of truth for _start_container / _exec / _host_sh /
# health-wait / _ensure_migration_image, and the @requires_containers marker.
from openhost_system_agent.tests.test_migration_container import _ENV_PYTHON
from openhost_system_agent.tests.test_migration_container import _PIXI
from openhost_system_agent.tests.test_migration_container import _REPO
from openhost_system_agent.tests.test_migration_container import _ensure_migration_image
from openhost_system_agent.tests.test_migration_container import _exec
from openhost_system_agent.tests.test_migration_container import _host_sh
from openhost_system_agent.tests.test_migration_container import _podman
from openhost_system_agent.tests.test_migration_container import _start_container
from openhost_system_agent.tests.test_migration_container import _wait_for_apply_unit
from openhost_system_agent.tests.test_migration_container import _wait_for_health
from openhost_system_agent.tests.test_migration_container import requires_containers

_LATEST_MIGRATION_VERSION = _latest_registry_version(_REGISTRY)


# ── Shared per-container setup helpers ───────────────────────────────


def _agent_path(container: str) -> str:
    """Resolve the openhost_system_agent console script inside the pixi env.

    The test image has no /usr/local/bin symlink, so the prod-style entrypoint
    is resolved from the default pixi env (mirrors TestApplyUpdateWalk).
    """
    which = _host_sh(container, f"cd {_REPO} && {_PIXI} run -e default which openhost_system_agent")
    return which.stdout.strip().splitlines()[-1]


def _run_agent(container: str, *subargs: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run ``sudo <agent> <subargs...>`` as root, never raising on nonzero.

    ``update apply`` is given ``--wait``: the walk runs detached as
    openhost-apply.service, so without it the command returns before any of the
    work has happened and every assertion below would race it. ``--wait`` blocks
    on the unit and surfaces the walk's outcome as an exit code, which is exactly
    the contract these tests assert on.
    """
    agent = _agent_path(container)
    args = list(subargs)
    if args[:2] == ["update", "apply"]:
        args.append("--wait")
    return _exec(container, "sudo", agent, *args, timeout=timeout, check=False)


def _agent_json(container: str, *subargs: str, timeout: int = 120) -> dict[str, object]:
    """Run an agent subcommand that prints a single JSON object and parse it."""
    r = _run_agent(container, *subargs, timeout=timeout)
    assert r.returncode == 0, f"agent {subargs} failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}"
    # The command prints exactly one JSON line; take the last non-empty line to
    # tolerate any incidental log noise on stdout.
    line = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1]
    parsed: dict[str, object] = json.loads(line)
    return parsed


def _head_sha(container: str) -> str:
    return _host_sh(container, f"cd {_REPO} && git rev-parse HEAD").stdout.strip()


def _current_tag(container: str) -> str:
    """Exact tag on HEAD, or '' if HEAD is not exactly on a release tag."""
    r = _host_sh(container, f"cd {_REPO} && git describe --tags --exact-match HEAD 2>/dev/null || true")
    return r.stdout.strip()


def _trust_origin(container: str, origin: str) -> None:
    """Let root (which runs the agent) read the host-owned file-based origin.

    Prod uses an HTTPS remote, so this dubious-ownership quirk is test-only.
    """
    _exec(container, "git", "config", "--global", "--add", "safe.directory", origin)


def _build_tagged_origin(
    container: str,
    origin: str,
    tags: list[str],
    checkout: str,
    *,
    pushed_from: int = 1,
) -> None:
    """Build a repo in _REPO tagged with ``tags``, cloned to a bare ``origin``.

    Mirrors the setup shell in TestApplyUpdateWalk: git init in the working
    repo, one empty commit per tag, clone --bare to ``origin`` at the first
    ``pushed_from - 1`` tags, then create + push the remaining tags to origin,
    delete them locally, and finally check out ``checkout`` (detached).

    The result is a host that physically has tags ``tags[:pushed_from-1]`` but
    must ``fetch`` the rest from origin — the real "N tags behind" state the
    walk resolves offline. Set ``pushed_from=1`` to push every tag.
    """
    lines = [
        f"cd {_REPO}",
        "rm -rf .git",
        "git -c init.defaultBranch=main init -q",
        "git config user.email t@e",
        "git config user.name t",
        "git add -A",
        "git commit -q -m r1",
        f"git tag {tags[0]}",
    ]
    # Local tags the host keeps before cloning the bare origin.
    for tag in tags[1 : pushed_from - 1]:
        lines.append(f"git commit -q --allow-empty -m {tag}")
        lines.append(f"git tag {tag}")
    lines.append(f"git clone -q --bare . {origin}")
    lines.append(f"git remote add origin {origin}")
    # Remaining tags exist only on origin until the agent fetches them.
    for tag in tags[max(pushed_from - 1, 1) :]:
        lines.append(f"git commit -q --allow-empty -m {tag}")
        lines.append(f"git tag {tag}")
        lines.append(f"git push -q origin {tag}")
        lines.append(f"git tag -d {tag}")
    # Fetch tags back from origin so ``checkout`` can name any tag we just
    # pushed-then-deleted locally (a pushed tag is not a remote-tracking branch,
    # so checkout DWIM won't resolve it otherwise). This lets a test sit the
    # host on the latest tag (up-to-date) as well as on an early one.
    lines.append(f"git fetch -q {origin} 'refs/tags/*:refs/tags/*'")
    lines.append(f"git checkout -q {checkout}")

    r = _host_sh(container, " && ".join(lines), timeout=180)
    assert r.returncode == 0, f"git setup failed:\n{r.stdout}\n{r.stderr}"
    _trust_origin(container, origin)


def _assert_healthy(container: str) -> None:
    try:
        body = _wait_for_health(container, timeout=120)
    except RuntimeError:
        journal = _podman(
            "exec", container, "journalctl", "-u", "openhost", "--no-pager", "-n", "50", timeout=10, check=False
        )
        raise RuntimeError(f"Health check failed. Journal:\n{journal.stdout}\n{journal.stderr}") from None
    assert '"ok"' in body or '"status"' in body


def _file_state(container: str, path: str) -> str:
    return _exec(
        container,
        _ENV_PYTHON,
        "-c",
        "import hashlib,json,sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); s=p.stat(); "
        "print(json.dumps([s.st_uid,s.st_gid,s.st_mode,s.st_mtime_ns,s.st_ctime_ns,"
        "hashlib.sha256(p.read_bytes()).hexdigest()]))",
        path,
    ).stdout


@contextmanager
def _without_dns_or_build_cache(container: str, *, repair_environment: bool = False) -> Iterator[Callable[[], None]]:
    cache = "/home/host/.cache/rattler/cache/uv-cache"
    parked_cache = "/home/host/.cache/rattler/cache/uv-cache.offline-test"
    resolver_backup = "/tmp/offline-test-resolv.conf"
    _exec(container, "cp", "/etc/resolv.conf", resolver_backup)
    _exec(container, "test", "!", "-e", parked_cache)
    had_cache = _exec(container, "test", "-d", cache, check=False).returncode == 0
    if had_cache:
        _exec(container, "mv", cache, parked_cache)

    def restore_dns() -> None:
        _exec(container, "cp", resolver_backup, "/etc/resolv.conf")

    try:
        # A documentation-only address cannot be answered by the running
        # instance's own loopback DNS listener.
        _exec(
            container,
            "sh",
            "-c",
            "printf 'nameserver 192.0.2.1\\noptions attempts:1 timeout:1\\n' > /etc/resolv.conf",
        )
        assert _exec(container, "getent", "ahostsv4", "pypi.org", check=False).returncode != 0
        yield restore_dns
    finally:
        # A timed-out exec client does not stop the detached updater. Quiesce
        # it and its failsafe before changing the cache or installed packages.
        _exec(container, "systemctl", "stop", "--no-block", "openhost-apply.service", check=False)
        _wait_for_apply_unit(container, timeout=120)
        _exec(container, "systemctl", "stop", "openhost", check=False)
        try:
            restore_dns()
        finally:
            _exec(container, "rm", "-rf", cache)
            if had_cache:
                _exec(container, "mv", parked_cache, cache)
        if repair_environment:
            # Failed editable builds can remove the app and its CLI. Repair
            # only after the automatic-recovery assertion has finished.
            repair = _host_sh(container, f"cd {_REPO} && {_PIXI} install", timeout=300)
            assert repair.returncode == 0, (
                f"test cleanup could not repair the environment:\n{repair.stdout}\n{repair.stderr}"
            )


@requires_containers
class TestApplyWalkE2E:
    """One container covers every apply/fetch/show_diff scenario.

    DEFINITION ORDER IS LOAD-BEARING: the migrations-only service check must
    run first (it observes the pristine post-migration state, before any walk
    or ``rm -rf .git``), and the walk tests must precede the edge cases. Do
    not reorder methods, and expect ``-k`` subsets / ``--ff`` to break this.
    """

    container = "openhost-e2e-applywalk"

    @classmethod
    def setup_class(cls) -> None:
        _ensure_migration_image()
        _start_container(cls.container)
        # Run the real migrations once so the baseline (v2) installs+enables the
        # openhost.service systemd unit. The apply-walk's final step does
        # `systemctl restart openhost`, which needs that unit to exist; without
        # this the walk would otherwise fail at the destination restart. This
        # also advances the migration log to the latest version, so the per-test
        # walks re-run migrations as fast no-ops.
        _exec(
            cls.container,
            _ENV_PYTHON,
            "-c",
            "from openhost_system_agent.migrations.runner import apply_system_migrations; apply_system_migrations()",
            timeout=300,
        )

    @classmethod
    def teardown_class(cls) -> None:
        _podman("rm", "-f", "-t", "0", cls.container, check=False, timeout=15)

    # ── Migrations alone enable the service ──────────────────────────

    def test_migrations_enable_openhost_service(self) -> None:
        """setup_class's migration run (independent of any walk) correctly installs
        and enables the openhost.service unit: a manual start works and the app
        serves /health. Must run first: the walked_to_latest fixture below is
        lazily instantiated, so no walk has happened yet at this point.
        """
        c = self.container
        _exec(c, "systemctl", "start", "openhost", timeout=30)
        time.sleep(2)
        result = _exec(c, "systemctl", "is-active", "openhost", timeout=10)
        assert result.stdout.strip() == "active", f"Service not active: {result.stdout}\n{result.stderr}"
        _assert_healthy(c)

    def test_restart_without_dns_or_build_cache(self) -> None:
        c = self.container
        # Fetch checks run a different ownership-repair path from service boot.
        # A local origin lets that path run while the package index is offline.
        _build_tagged_origin(c, "/tmp/origin_offline_check.git", ["v1"], checkout="v1")
        agent = _agent_path(c)
        before = _file_state(c, f"{_REPO}/pyproject.toml")
        _exec(c, "systemctl", "stop", "openhost")
        with _without_dns_or_build_cache(c):
            check = _exec(c, "sudo", agent, "update", "fetch", check=False)
            assert check.returncode == 0, f"local update check failed:\n{check.stdout}\n{check.stderr}"
            assert _file_state(c, f"{_REPO}/pyproject.toml") == before
            _exec(c, "systemctl", "restart", "openhost")
            _assert_healthy(c)
            assert _file_state(c, f"{_REPO}/pyproject.toml") == before

    @pytest.mark.parametrize("reclaimer", ["boot", "system-agent"])
    def test_reclaim_preserves_correct_files_and_does_not_follow_symlinks(self, reclaimer: str) -> None:
        c = self.container
        metadata = f"{_REPO}/.pixi/envs/default/conda-meta/pixi"
        link = f"{_REPO}/.reclaim-test-link"
        group_only = f"{_REPO}/.reclaim-test-group"
        outside = "/tmp/reclaim-outside-target"
        outside_file = f"{outside}/keep-root-owned"
        _exec(c, "systemctl", "stop", "openhost")
        _exec(c, "mkdir", outside)
        _exec(c, "touch", outside_file, group_only)
        _exec(c, "chown", "root:root", outside, outside_file, metadata, _REPO)
        _exec(c, "chown", "host:root", group_only)
        _exec(c, "ln", "-s", outside, link)
        before = _file_state(c, f"{_REPO}/pyproject.toml")
        outside_before = _file_state(c, outside_file)
        command = (
            ["/usr/local/bin/openhost-reclaim-pixi"]
            if reclaimer == "boot"
            else [
                _ENV_PYTHON,
                "-c",
                "from openhost_system_agent.reclaim import reclaim_host_ownership; reclaim_host_ownership()",
            ]
        )
        try:
            for _ in range(2):
                _exec(c, *command)
                assert _file_state(c, f"{_REPO}/pyproject.toml") == before
                assert _file_state(c, outside_file) == outside_before
                for path in (_REPO, metadata, group_only, link):
                    assert _exec(c, "stat", "-c", "%U:%G", path).stdout.strip() == "host:host"
        finally:
            _exec(c, "chown", "host:host", _REPO, metadata)
            _exec(c, "rm", "-f", link, group_only)
            _exec(c, "rm", "-rf", outside)

    def test_crash_recovers_offline_and_repairs_environment_ownership(self) -> None:
        c = self.container
        metadata = f"{_REPO}/.pixi/envs/default/conda-meta/pixi"
        owner = _exec(c, "stat", "-c", "%u:%g", metadata).stdout.strip()
        mode = _exec(c, "stat", "-c", "%a", metadata).stdout.strip()
        _exec(c, "systemctl", "stop", "openhost")
        try:
            with _without_dns_or_build_cache(c):
                _exec(c, "systemctl", "reset-failed", "openhost")
                _exec(c, "systemctl", "start", "openhost")
                _assert_healthy(c)
                old_pid = _exec(c, "systemctl", "show", "openhost", "-p", "MainPID", "--value").stdout.strip()
                old_restarts = int(_exec(c, "systemctl", "show", "openhost", "-p", "NRestarts", "--value").stdout)
                _exec(c, "chown", "root:root", metadata)
                _exec(c, "chmod", "0600", metadata)
                _exec(c, "systemctl", "kill", "--kill-whom=main", "--signal=SIGKILL", "openhost")

                # The old Python child can answer briefly after Pixi is killed.
                # Require a new main process before accepting HTTP readiness.
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    pid = _exec(c, "systemctl", "show", "openhost", "-p", "MainPID", "--value").stdout.strip()
                    if pid not in ("0", old_pid):
                        break
                    time.sleep(1)
                else:
                    pytest.fail("systemd did not automatically start a new process")
                _assert_healthy(c)
                restarts = int(_exec(c, "systemctl", "show", "openhost", "-p", "NRestarts", "--value").stdout)
                assert restarts == old_restarts + 1
                assert _exec(c, "stat", "-c", "%U:%G", metadata).stdout.strip() == "host:host"
                assert _exec(c, "getent", "ahostsv4", "pypi.org", check=False).returncode != 0
        finally:
            _exec(c, "chown", owner, metadata)
            _exec(c, "chmod", mode, metadata)

    # ── Multi-tag walk in a single invocation, then idempotent re-apply ──

    @pytest.fixture(scope="class")
    @classmethod
    def walked_to_latest(cls) -> str:
        """Build the origin and walk v1 → v3 once for the class; return the resulting HEAD sha."""
        c = cls.container
        # Host physically has only v1; v2 and v3 live on origin.
        _build_tagged_origin(c, "/tmp/origin_multitag.git", ["v1", "v2", "v3"], checkout="v1")

        # Host is behind before the walk applies.
        assert _agent_json(c, "update", "fetch")["state"] == "BEHIND_REMOTE"

        apply = _run_agent(c, "update", "apply")
        assert apply.returncode == 0, f"update apply failed (exit {apply.returncode}):\n{apply.stdout}\n{apply.stderr}"

        # Single invocation ended on the latest tag, exactly v3.
        assert _current_tag(c) == "v3", f"HEAD not on v3: {_current_tag(c)!r}"

        # openhost was restarted by the walk and serves /health.
        _assert_healthy(c)

        return _head_sha(c)

    def test_single_apply_walks_all_tags_to_latest(self, walked_to_latest: str) -> None:
        c = self.container

        # Migration log still reads the latest known version (setup_class already
        # advanced it, so the walk's migrations re-ran as no-ops; the point is the
        # walk did not regress or corrupt it).
        log = _exec(c, "cat", "/etc/openhost/migrations.jsonl")
        assert f'"version":{_LATEST_MIGRATION_VERSION}' in log.stdout.replace(" ", ""), (
            f"log did not reach v{_LATEST_MIGRATION_VERSION}:\n{log.stdout}"
        )

        # pixi ran install during the walk (version is whatever the image ships;
        # we only assert install did not break the toolchain).
        pixi_after = _host_sh(c, f"{_PIXI} --version").stdout
        assert pixi_after.strip(), f"pixi broken after walk (after={pixi_after!r})"

    def test_re_apply_is_noop_on_latest(self, walked_to_latest: str) -> None:
        """After the class-level walk to v3, a second `update apply` is a healthy no-op."""
        c = self.container

        second = _run_agent(c, "update", "apply")
        assert second.returncode == 0, f"second apply failed:\n{second.stdout}\n{second.stderr}"
        assert _head_sha(c) == walked_to_latest, "re-apply moved HEAD despite being on latest"
        assert _current_tag(c) == "v3"
        assert _agent_json(c, "update", "fetch")["state"] == "UP_TO_DATE"
        # openhost was restarted again by the second apply; confirm the service
        # comes back active. Poll `systemctl is-active` (the restart is what the
        # walk drives) rather than the app-level /health, which can cold-start
        # slowly and is already covered by the class-level _assert_healthy above.
        deadline = time.time() + 120
        active = ""
        while time.time() < deadline:
            active = _exec(c, "systemctl", "is-active", "openhost", check=False).stdout.strip()
            if active == "active":
                break
            time.sleep(2)
        assert active == "active", (
            f"openhost not active after second apply (state={active!r}):\n"
            f"{_exec(c, 'journalctl', '-u', 'openhost', '--no-pager', '-n', '40', check=False).stdout}"
        )

    # ── Dirty tree / no tags / target-ref pin / show_diff (independent) ──

    def test_apply_rejects_dirty_tree(self) -> None:
        """An uncommitted change makes `update apply` fail without moving HEAD."""
        c = self.container
        _build_tagged_origin(c, "/tmp/origin_dirty.git", ["v1", "v2"], checkout="v1")

        # Introduce an uncommitted change to a tracked file. Use the agent's
        # README (not pyproject.toml) so we don't corrupt the TOML that
        # `pixi run` parses when resolving the agent console script.
        r = _host_sh(
            c,
            f"cd {_REPO} && echo dirty >> openhost_system_agent/README.md && git status --porcelain",
        )
        assert r.stdout.strip(), "working tree should be dirty for this test"

        head_before = _head_sha(c)
        apply = _run_agent(c, "update", "apply")

        # Nonzero exit with a clear message, and HEAD did not move.
        assert apply.returncode != 0, f"apply should reject a dirty tree:\n{apply.stdout}\n{apply.stderr}"
        combined = (apply.stdout + apply.stderr).lower()
        assert "uncommitted" in combined or "dirty" in combined, (
            f"unclear dirty error:\n{apply.stdout}\n{apply.stderr}"
        )
        assert _head_sha(c) == head_before, "apply moved HEAD despite dirty tree"
        assert _current_tag(c) == "v1"

        # Restore the working tree so the next scenario's `rm -rf .git` +
        # `git add -A` doesn't silently commit this test's leftover dirty
        # change into a fresh repo.
        _host_sh(c, f"cd {_REPO} && git checkout -- openhost_system_agent/README.md")

    def test_apply_without_tags_fails(self) -> None:
        """No v* tags and no target ref: `update apply` fails with 'No tags'."""
        c = self.container
        origin = "/tmp/origin_notags.git"
        # A repo + bare origin with a single untagged commit and no target ref.
        setup = " && ".join(
            [
                f"cd {_REPO}",
                "rm -rf .git",
                "git -c init.defaultBranch=main init -q",
                "git config user.email t@e",
                "git config user.name t",
                "git add -A",
                "git commit -q -m r1",
                f"git clone -q --bare . {origin}",
                f"git remote add origin {origin}",
            ]
        )
        r = _host_sh(c, setup, timeout=120)
        assert r.returncode == 0, f"git setup failed:\n{r.stdout}\n{r.stderr}"
        _trust_origin(c, origin)

        head_before = _head_sha(c)
        apply = _run_agent(c, "update", "apply", timeout=120)

        assert apply.returncode != 0, f"apply should fail with no tags:\n{apply.stdout}\n{apply.stderr}"
        assert "no tags" in (apply.stdout + apply.stderr).lower(), (
            f"expected a 'No tags found' error:\n{apply.stdout}\n{apply.stderr}"
        )
        assert _head_sha(c) == head_before, "apply moved HEAD despite failing"

    def test_walk_ends_on_pinned_ref_after_tags(self) -> None:
        """A pinned target ref becomes the final hop after the tags are walked."""
        c = self.container
        origin = "/tmp/origin_targetref.git"

        # Build v1, v2 on main, then a 'feature' branch one commit AHEAD of v2.
        # Push everything to origin, drop v2 + feature locally so the host must
        # fetch them, then sit the host on v1.
        setup = " && ".join(
            [
                f"cd {_REPO}",
                "rm -rf .git",
                "git -c init.defaultBranch=main init -q",
                "git config user.email t@e",
                "git config user.name t",
                "git add -A",
                "git commit -q -m r1",
                "git tag v1",
                "git commit -q --allow-empty -m r2",
                "git tag v2",
                "git checkout -q -b feature",
                "git commit -q --allow-empty -m 'feature tip'",
                "git checkout -q main",
                f"git clone -q --bare . {origin}",
                f"git remote add origin {origin}",
                "git checkout -q v1",
            ]
        )
        r = _host_sh(c, setup, timeout=180)
        assert r.returncode == 0, f"git setup failed:\n{r.stdout}\n{r.stderr}"
        _trust_origin(c, origin)

        # Pin the destination to the feature branch tip (ahead of the latest
        # tag). Write the config as the host user (owns the repo); root git
        # would refuse with "dubious ownership" until the agent trusts it.
        pin = _host_sh(c, f"cd {_REPO} && git config openhost.target-ref feature")
        assert pin.returncode == 0, f"failed to set target-ref pin:\n{pin.stdout}\n{pin.stderr}"

        # Resolve the expected pinned commit from origin for the final assert.
        # Use --verify --quiet so an unresolved ref fails silently (git otherwise
        # echoes the ref name to stdout), and take the last line to be safe.
        feature_sha = _host_sh(
            c,
            f"cd {_REPO} && (git rev-parse --verify --quiet origin/feature "
            f"|| git rev-parse --verify --quiet feature) | tail -n1",
        ).stdout.strip()
        # Sanity: the pinned tip is NOT the v2 commit (it's one commit ahead).
        v2_sha = _host_sh(c, f"cd {_REPO} && git rev-parse v2").stdout.strip()
        assert feature_sha and feature_sha != v2_sha, f"feature tip should lead v2 (feature={feature_sha} v2={v2_sha})"

        apply = _run_agent(c, "update", "apply")
        assert apply.returncode == 0, f"pinned apply failed:\n{apply.stdout}\n{apply.stderr}"

        # The walk stepped through the tags and ended on the pinned ref's commit.
        assert _head_sha(c) == feature_sha, f"HEAD not on pinned feature tip: {_head_sha(c)!r} != {feature_sha!r}"
        assert _current_tag(c) == "", "HEAD should be on the branch tip, not exactly on a tag"
        _assert_healthy(c)

    def test_show_diff_lists_pending_commits(self) -> None:
        """`update show_diff` lists the pending commits with correct refs."""
        c = self.container
        origin = "/tmp/origin_showdiff.git"

        # Host on v1; origin has v2 reached by two commits past v1.
        setup = " && ".join(
            [
                f"cd {_REPO}",
                "rm -rf .git",
                "git -c init.defaultBranch=main init -q",
                "git config user.email t@e",
                "git config user.name t",
                "git add -A",
                "git commit -q -m r1",
                "git tag v1",
                f"git clone -q --bare . {origin}",
                f"git remote add origin {origin}",
                "git commit -q --allow-empty -m 'pending one'",
                "git commit -q --allow-empty -m 'pending two'",
                "git tag v2",
                "git push -q origin v2",
                "git tag -d v2",
                "git checkout -q v1",
            ]
        )
        r = _host_sh(c, setup, timeout=180)
        assert r.returncode == 0, f"git setup failed:\n{r.stdout}\n{r.stderr}"
        _trust_origin(c, origin)

        # fetch first so origin's v2 is present locally for the diff.
        assert _agent_json(c, "update", "fetch")["state"] == "BEHIND_REMOTE"

        # cappa exposes the subcommand as "show-diff" (it maps the Python
        # method name show_diff to a hyphenated CLI name).
        diff = _agent_json(c, "update", "show-diff")
        assert diff["current_ref"] == "v1", f"unexpected current_ref: {diff!r}"
        assert diff["remote_ref"] == "v2", f"unexpected remote_ref: {diff!r}"
        commits = cast("list[dict[str, str]]", diff["commits"])
        assert len(commits) == 2, f"expected 2 pending commits, got: {commits!r}"
        messages = [commit["message"] for commit in commits]
        assert "pending one" in messages and "pending two" in messages, f"missing pending messages: {messages!r}"

    def test_failed_update_recovers_when_package_index_returns(self) -> None:
        c = self.container
        origin = "/tmp/origin_failed_install.git"
        _build_tagged_origin(c, origin, ["v1"], checkout="v1")
        # The destination genuinely changes packaging metadata. An empty tag
        # no longer needs rebuilding when ownership repair leaves ctime alone.
        _exec(
            c,
            "sudo",
            "-u",
            "host",
            "-H",
            _ENV_PYTHON,
            "-c",
            "import re; from pathlib import Path; p=Path('pyproject.toml'); "
            "text,count=re.subn(r'(?m)^description = .+$', "
            "'description = \"Updated package metadata\"', p.read_text(), count=1); "
            "assert count == 1; p.write_text(text)",
        )
        publish = _host_sh(
            c,
            f"cd {_REPO} && git add pyproject.toml && git commit -q -m metadata "
            "&& git tag v2 && git push -q origin v2 && git checkout -q v1",
        )
        assert publish.returncode == 0, f"metadata update fixture failed:\n{publish.stdout}\n{publish.stderr}"
        # Resolve the installed entrypoint while online. Resolving it through
        # pixi after the fault would synchronize the environment before apply.
        agent = _agent_path(c)
        _exec(c, "systemctl", "reset-failed", "openhost")
        _exec(c, "systemctl", "start", "openhost")
        _assert_healthy(c)
        with _without_dns_or_build_cache(c, repair_environment=True) as restore_dns:
            apply = _exec(c, "sudo", agent, "update", "apply", "--wait", timeout=180, check=False)
            assert apply.returncode != 0, "update unexpectedly succeeded with PyPI unavailable"
            assert _current_tag(c) == "v2", "update failed before checking out the destination"
            progress = _exec(
                c,
                "cat",
                "/home/host/.openhost/local_compute_space/persistent_data/openhost/updater/progress.jsonl",
            ).stdout
            entries = [json.loads(line) for line in progress.splitlines() if line.strip()]
            assert any(entry["phase"] == "install" for entry in entries)
            assert entries[-1]["phase"] == "failed"
            assert "Dependency install failed" in entries[-1]["message"]

            # Only the unavailable dependency source recovers. Do not
            # manually install, start the service, or clear its limiter.
            restore_dns()
            _assert_healthy(c)
