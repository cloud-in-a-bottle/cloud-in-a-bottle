"""Container-based tests against a real Ubuntu+systemd host.

``TestMigrationContainer`` runs the migrations directly and checks openhost
comes up. ``TestApplyUpdateWalk`` drives the *real* ``update apply`` entrypoint
end to end — fetch tags, step onto the next release tag, run migrations (which
upgrade pixi), reinstall deps, and restart openhost — which is the path the
phased-update framework exists to make safe.

Requires podman and the --run-containers flag.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.registry import latest_registry_version

requires_containers = pytest.mark.requires_containers

_IMAGE_NAME = "openhost-migration-test:latest"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_DOCKERFILE = Path(__file__).resolve().parent / "Dockerfile.migration_test"
_PIXI = "/home/host/.pixi/bin/pixi"
_REPO = "/home/host/openhost"
# The env interpreter, invoked directly the way the prod console script is —
# NOT via `pixi run`, which re-syncs PyPI as the calling user and would leave
# root-owned files in a host-owned env.
_ENV_PYTHON = "/home/host/openhost/.pixi/envs/default/bin/python"


def _podman(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _exec(container: str, *args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _podman("exec", container, *args, timeout=timeout, check=check)


def _host_sh(container: str, script: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet as the unprivileged 'host' user via a login shell."""
    return _exec(container, "su", "-", "host", "-c", script, timeout=timeout)


def _start_container(name: str) -> None:
    _podman("rm", "-f", "-t", "0", name, check=False, timeout=15)
    _podman(
        "run",
        "-d",
        "--systemd=always",
        "--tmpfs=/run",
        "--tmpfs=/run/lock",
        "--cap-add=NET_ADMIN",
        "--name",
        name,
        _IMAGE_NAME,
        timeout=30,
    )
    _wait_for_systemd(name)


def _wait_for_systemd(container: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    state = ""
    while time.time() < deadline:
        result = _podman("exec", container, "systemctl", "is-system-running", timeout=10, check=False)
        state = result.stdout.strip()
        if state in ("running", "degraded"):
            return
        time.sleep(1)
    raise RuntimeError(f"systemd did not reach running state within {timeout}s (last: {state!r})")


def _wait_for_health(container: str, timeout: int = 60) -> str:
    deadline = time.time() + timeout
    last_stderr = ""
    while time.time() < deadline:
        result = _podman("exec", container, "curl", "-sf", "http://localhost:8080/health", timeout=10, check=False)
        if result.returncode == 0:
            return result.stdout
        last_stderr = result.stderr.strip()
        time.sleep(2)
    raise RuntimeError(f"/health did not respond within {timeout}s (last stderr: {last_stderr!r})")


def _dump_openhost_journal(container: str) -> str:
    journal = _podman(
        "exec", container, "journalctl", "-u", "openhost", "--no-pager", "-n", "50", timeout=10, check=False
    )
    return f"{journal.stdout}\n{journal.stderr}"


@pytest.fixture(scope="module", autouse=True)
def _migration_image() -> object:
    """Build the test image once for the module; reuse it across containers."""
    if _podman("image", "exists", _IMAGE_NAME, check=False).returncode != 0:
        _podman("build", "-t", _IMAGE_NAME, "-f", str(_DOCKERFILE), str(_REPO_ROOT), timeout=600)
    yield
    _podman("rmi", "-f", _IMAGE_NAME, check=False, timeout=30)


@requires_containers
class TestMigrationContainer:
    container = "openhost-migration-test"

    @classmethod
    def setup_class(cls) -> None:
        _start_container(cls.container)

    @classmethod
    def teardown_class(cls) -> None:
        _podman("rm", "-f", "-t", "0", cls.container, check=False, timeout=15)

    def test_migrations_apply(self) -> None:
        result = _exec(
            self.container,
            _ENV_PYTHON,
            "-c",
            "from openhost_system_agent.migrations.runner import apply_system_migrations; "
            "print(apply_system_migrations())",
            timeout=300,
        )
        assert result.returncode == 0, f"Migration failed:\n{result.stderr}"

    def test_openhost_service_starts(self) -> None:
        _exec(self.container, "systemctl", "start", "openhost", timeout=30)
        time.sleep(2)
        result = _exec(self.container, "systemctl", "is-active", "openhost", timeout=10)
        assert result.stdout.strip() == "active", f"Service not active: {result.stdout}\n{result.stderr}"

    def test_health_endpoint(self) -> None:
        try:
            body = _wait_for_health(self.container, timeout=120)
        except RuntimeError:
            raise RuntimeError(f"Health check failed. Journal:\n{_dump_openhost_journal(self.container)}") from None
        assert '"ok"' in body or '"status"' in body


@requires_containers
class TestApplyUpdateWalk:
    """End-to-end: `update apply` walks onto a new tag, upgrades pixi, restarts."""

    container = "openhost-apply-walk-test"

    @classmethod
    def setup_class(cls) -> None:
        _start_container(cls.container)

    @classmethod
    def teardown_class(cls) -> None:
        _podman("rm", "-f", "-t", "0", cls.container, check=False, timeout=15)

    def test_update_apply_walks_tags_and_upgrades_pixi(self) -> None:
        c = self.container

        # Build a clean repo tagged v1, plus a file-based origin holding a v2
        # the host doesn't have yet, then sit the host on v1 (detached). This
        # is the "one tag behind" state `update apply` must resolve offline.
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
                "git clone -q --bare . /tmp/origin.git",
                "git remote add origin /tmp/origin.git",
                "git commit -q --allow-empty -m r2",
                "git tag v2",
                "git push -q origin v2",
                "git tag -d v2",
                "git checkout -q v1",
            ]
        )
        r = _host_sh(c, setup, timeout=180)
        assert r.returncode == 0, f"git setup failed:\n{r.stdout}\n{r.stderr}"

        # The file-based origin is host-owned, so let root (which runs the
        # agent) read it. Prod uses an HTTPS remote, so this dubious-ownership
        # quirk is test-only; the agent trusts the working repo on its own.
        _exec(c, "git", "config", "--global", "--add", "safe.directory", "/tmp/origin.git")

        # Precondition: the image ships the pre-migration pixi.
        before = _host_sh(c, f"{_PIXI} --version")
        assert "0.69.0" in before.stdout, f"unexpected starting pixi: {before.stdout!r}"

        # Run the real entrypoint as root (no /usr/local/bin symlink in the
        # test image, so resolve the console script from the pixi env).
        which = _host_sh(c, f"cd {_REPO} && {_PIXI} run -e default which openhost_system_agent")
        agent = which.stdout.strip().splitlines()[-1]
        apply = _exec(c, "sudo", agent, "update", "apply", timeout=600, check=False)
        assert apply.returncode == 0, f"update apply failed (exit {apply.returncode}):\n{apply.stdout}\n{apply.stderr}"

        # The pixi-version migration upgraded pixi to the pinned version.
        after = _host_sh(c, f"{_PIXI} --version")
        assert "0.70.2" in after.stdout, f"pixi not upgraded: {after.stdout!r}"

        # The walk stepped HEAD onto v2.
        tag = _host_sh(c, f"cd {_REPO} && git describe --tags --exact-match HEAD")
        assert tag.stdout.strip() == "v2", f"HEAD not on v2: {tag.stdout!r}"

        # The migration log advanced through to the registry's highest version.
        # Reference the registry so this can't drift when a migration is added.
        latest = latest_registry_version(REGISTRY)
        log = _exec(c, "cat", "/etc/openhost/migrations.jsonl")
        assert f'"version":{latest}' in log.stdout.replace(" ", ""), f"log did not reach v{latest}:\n{log.stdout}"

        # openhost was restarted by the walk and serves /health.
        try:
            body = _wait_for_health(c, timeout=120)
        except RuntimeError:
            raise RuntimeError(f"Health check failed. Journal:\n{_dump_openhost_journal(c)}") from None
        assert '"ok"' in body or '"status"' in body
