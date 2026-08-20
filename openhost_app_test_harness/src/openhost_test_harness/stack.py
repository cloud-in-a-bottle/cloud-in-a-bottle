"""Run an app under test on a real local Cloud in a Bottle stack.

Typical use in an app repo's ``conftest.py``::

    import pytest
    from openhost_test_harness import OpenhostStack

    @pytest.fixture(scope="session")
    def stack():
        # app_dir is discovered by walking up from the cwd to the nearest
        # ciab.toml; pass app_dir=... explicitly to override.
        with OpenhostStack() as s:
            yield s

This starts the real Cloud in a Bottle router (HTTP-only, on a ``*.localhost`` zone) and deploys the
app through the real install path, so routing, auth, identity env vars, and the v2 service
interface all behave exactly as in production.

Requirements: rootless podman (a running ``podman machine`` on macOS), python >= 3.12.
On Linux, container→router traffic additionally needs the ``openhost0`` dummy interface +
``host_containers_internal_ip`` containers.conf setting that Cloud in a Bottle servers get from
ansible/tasks/containers.yml (see the openhost repo's CI workflow for a minimal version);
without it, service calls from apps hang.
"""

import contextlib
import logging
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any
from typing import Self

import attr
import requests
from playwright.sync_api import Page

from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.manifest import MANIFEST_FILENAMES
from compute_space.core.manifest import find_manifest_path
from compute_space.core.manifest import parse_manifest
from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup
from compute_space.tests.local_stack import deploy_app
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.utils import managed_router
from openhost_test_harness.consumer_app import write_consumer_app_dir

logger = logging.getLogger(__name__)


def find_manifest_dir(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: cwd) to the nearest directory containing an app manifest.

    Looks for the canonical ``ciab.toml`` first and falls back to the legacy
    ``openhost.toml`` (see ``MANIFEST_FILENAMES``).
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if find_manifest_path(str(candidate)) is not None:
            return candidate
    raise FileNotFoundError(f"No {MANIFEST_FILENAMES[0]} found walking up from {cur}")


def _resolve_app_dir(value: Path | str | None) -> Path:
    if value is None:
        return find_manifest_dir()
    return Path(value).resolve()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _data_base_dir() -> Path:
    """Base dir for per-run stack data.

    Pinned under $HOME rather than the system tempdir: on macOS podman runs in a VM that
    only shares a fixed set of host paths (/Users, /private, /var/folders), and bind-mount
    sources must be visible there.  A relocated TMPDIR breaks mounts with
    ``statfs ... no such file or directory``.
    """
    base = Path.home() / ".cache" / "openhost-test-harness"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _normalize_service_url(url: str) -> str:
    return url.removeprefix("https://").removeprefix("http://").rstrip("/")


def _copy_git_listed_files(src_dir: Path, dest_dir: Path) -> None:
    """Copy ``src_dir``'s tracked + untracked-unignored files into ``dest_dir``."""
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=src_dir,
        capture_output=True,
        check=True,
    )
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode()
        src = src_dir / rel
        if not src.is_file():  # staged deletions still appear in ls-files
            continue
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _snapshot_working_tree(app_dir: Path, dest: Path) -> None:
    """Copy ``app_dir``'s git working tree to ``dest``: tracked + untracked files,
    minus gitignored ones and ``.git`` itself. Submodule working trees are included
    (``git ls-files`` lists a submodule as a single gitlink, not its files).

    The router clones git repos at HEAD, which would silently deploy stale code while
    you iterate; deploying a snapshot makes the app under test reflect uncommitted
    changes, while gitignored build artefacts (.venv, node_modules, ...) stay out of
    the build context.
    """
    _copy_git_listed_files(app_dir, dest)
    submodules = subprocess.run(
        ["git", "submodule", "foreach", "--quiet", "--recursive", "echo $displaypath"],
        cwd=app_dir,
        capture_output=True,
        check=True,
        text=True,
    )
    for sub_rel in submodules.stdout.splitlines():
        sub_rel = sub_rel.strip()
        if sub_rel:
            _copy_git_listed_files(app_dir / sub_rel, dest / sub_rel)


def _warn_if_linux_gateway_missing() -> None:
    """On Linux, service calls from apps need the host_containers_internal_ip setup."""
    if sys.platform != "linux":
        return
    conf = Path.home() / ".config" / "containers" / "containers.conf"
    if not conf.exists() or "host_containers_internal_ip" not in conf.read_text():
        logger.warning(
            "host_containers_internal_ip is not configured in %s — app→router service calls "
            "will hang. Set up the openhost0 dummy interface and containers.conf override "
            "(see ansible/tasks/containers.yml or the openhost CI workflow).",
            conf,
        )


@attr.s(auto_attribs=True, frozen=True)
class ServiceCallResult:
    """What the router's service proxy returned to the consumer app."""

    status: int
    body: Any


@attr.s(auto_attribs=True, frozen=True)
class ServiceConsumer:
    """A deployed synthetic consumer app; use ``call`` to exercise a provider's service."""

    stack: "OpenhostStack"
    name: str
    shortname: str
    app_id: str

    def call(self, path: str, payload: Any = None, method: str = "POST") -> ServiceCallResult:
        """Call the consumed service through the real router service proxy.

        ``path`` is appended to the provider's endpoint (e.g. ``"get"``); it must be
        non-empty or the router's service-call route doesn't match.
        """
        r = self.stack.owner_session.post(
            f"{self.stack.url_for(self.name)}/call-service",
            json={"shortname": self.shortname, "path": path, "payload": payload, "method": method},
            timeout=30,
        )
        assert r.status_code == 200, f"consumer /call-service failed: {r.status_code}: {r.text[:300]}"
        result = r.json()
        return ServiceCallResult(status=result["service_status"], body=result["service_body"])


@attr.define
class OpenhostStack:
    """Build, deploy, and front a Cloud in a Bottle app on a real local router for tests.

    Use as a context manager.  ``app_dir`` defaults to the nearest directory containing a
    ``ciab.toml``, found by walking up from the current working directory.

    - ``stack.url`` — the app through the router (subdomain routing, real auth)
    - ``stack.owner_session`` — a requests.Session authenticated as the zone owner; its
      cookie is scoped to the zone domain, so it works on ``stack.url`` and all other
      app URLs
    - ``stack.app_url`` — direct to the app container, bypassing the router (for tests
      that forge ``X-OpenHost-*`` headers or check unauthenticated behavior)
    """

    app_dir: Path = attr.field(default=None, converter=_resolve_app_dir)
    app_name: str | None = None
    """Deploy under this name; defaults to the ciab.toml [app].name."""
    grant_manifest_permissions: bool = True
    """Auto-grant the grants declared in the app's [[services.v2.consumes]] at install."""
    pre_deploy: Callable[["OpenhostStack"], None] | None = None
    """Called once the router is up and the owner session exists, before the app under test
    deploys.  Use it to deploy provider apps and seed config the app needs at startup (e.g.
    a secrets provider plus its values) — mirroring an owner preparing a server before
    installing the app.  ``stack.url``/``stack.app_id`` are not available yet inside the hook."""
    zone_name: str = "harness"
    deploy_timeout: float = 300.0

    _data_dir: Path = attr.field(init=False, default=None)
    _exit_stack: contextlib.ExitStack | None = attr.field(init=False, default=None)
    _local_stack: LocalStack = attr.field(init=False, default=None)
    _owner: requests.Session | None = attr.field(init=False, default=None)
    _app_id: str = attr.field(init=False, default="")

    def __enter__(self) -> Self:
        _warn_if_linux_gateway_missing()
        self._data_dir = Path(tempfile.mkdtemp(prefix="stack-", dir=_data_base_dir()))
        port = _free_port()
        config = make_local_stack_config(
            data_root_dir=str(self._data_dir),
            port=port,
            zone_name=self.zone_name,
            default_apps=[],
        )
        self._local_stack = LocalStack(config=config)
        self._exit_stack = contextlib.ExitStack()
        try:
            self._exit_stack.enter_context(managed_router(config))
            self._owner = complete_setup(self._local_stack)
            if self.pre_deploy is not None:
                self.pre_deploy(self)
            deploy_src = self.app_dir
            if (self.app_dir / ".git").exists():
                deploy_src = self._data_dir / "app-under-test"
                _snapshot_working_tree(self.app_dir, deploy_src)
            self._app_id = self.deploy_app(
                f"file://{deploy_src}",
                app_name=self.app_name,
                grant_manifest_permissions=self.grant_manifest_permissions,
            )
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exit_stack is not None:
            self._exit_stack.close()
            self._exit_stack = None
        if self._local_stack is not None:
            self._local_stack.remove_deployed_app_containers()
        if self._data_dir is not None:
            shutil.rmtree(self._data_dir, ignore_errors=True)

    # ─── URLs and sessions ───

    @property
    def local_stack(self) -> LocalStack:
        return self._local_stack

    @property
    def owner_session(self) -> requests.Session:
        """Session authenticated as the zone owner (cookie valid for all app subdomains)."""
        assert self._owner is not None, "OpenhostStack must be entered first"
        return self._owner

    def playwright_login(self, page: Page) -> Page:
        """Log ``page`` in as the zone owner through the real /login form, and return it.

        Drives the actual login flow — navigate to the router's login page, submit the
        owner password, wait for the post-login redirect — so the session cookie lands on
        the page's context and the page (and any others in that context) can then reach
        owner-only routes::

            def test_dashboard(stack, page):
                stack.playwright_login(page)
                page.goto(stack.url)
        """
        page.goto(f"{self.router_url}/login")
        page.fill("input[name=password]", self.local_stack.owner_password)
        page.click("input[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url)
        return page

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def router_url(self) -> str:
        return self._local_stack.router_url

    @property
    def url(self) -> str:
        """The app under test, through the router (real subdomain routing and auth)."""
        return self.url_for(self._deployed_app_name)

    def url_for(self, app_name: str) -> str:
        """Any deployed app's URL through the router."""
        return self._local_stack.app_url(app_name)

    @property
    def app_url(self) -> str:
        """The app under test's container directly, bypassing the router."""
        return f"http://127.0.0.1:{self._local_port(self._deployed_app_name)}"

    @property
    def _deployed_app_name(self) -> str:
        return self.app_name or parse_manifest(str(self.app_dir)).name

    def _local_port(self, app_name: str) -> int:
        db = sqlite3.connect(self._local_stack.config.db_path)
        try:
            row = db.execute("SELECT local_port FROM apps WHERE name = ?", (app_name,)).fetchone()
        finally:
            db.close()
        assert row is not None, f"app {app_name!r} not found in router db"
        return int(row[0])

    # ─── Deploying more apps and wiring services ───

    def deploy_app(
        self,
        repo_url: str,
        app_name: str | None = None,
        grant_manifest_permissions: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Deploy another app (e.g. a provider your app consumes).  Returns its app_id.

        ``repo_url`` can be a git URL or ``file:///path`` (non-git dirs are copied).
        """
        return deploy_app(
            self.owner_session,
            self._local_stack,
            repo_url,
            app_name=app_name,
            grant_manifest_permissions=grant_manifest_permissions,
            timeout=timeout if timeout is not None else self.deploy_timeout,
        )

    def grant(self, app_id: str, service: str, grant: Grant) -> None:
        """Owner-grant a global-scoped v2 permission to ``app_id`` for ``service``."""
        r = self.owner_session.post(
            f"{self.router_url}/api/permissions/v2/grant_global_scoped",
            json={"app_id": app_id, "service_url": _normalize_service_url(service), "grant": grant},
            timeout=10,
        )
        assert r.status_code == 200, f"grant failed: {r.status_code}: {r.text[:300]}"

    def deploy_service_consumer(
        self,
        service: str,
        shortname: str = "svc",
        version: str = ">=0.0.0",
        grants: list[Grant] | None = None,
        name: str | None = None,
        grant_manifest_permissions: bool = False,
    ) -> ServiceConsumer:
        """Deploy a synthetic consumer of ``service`` — for testing apps that *provide* a service.

        The consumer declares ``grants`` in its manifest; they are only actually granted when
        ``grant_manifest_permissions=True`` (or later via ``stack.grant``), so the
        permission-denied path stays testable.
        """
        consumer_name = name or f"consumer-{shortname}"
        app_dir = self._data_dir / "consumers" / consumer_name
        write_consumer_app_dir(
            app_dir,
            name=consumer_name,
            service=_normalize_service_url(service),
            shortname=shortname,
            version=version,
            grants=grants or [],
        )
        app_id = self.deploy_app(
            f"file://{app_dir}",
            grant_manifest_permissions=grant_manifest_permissions,
        )
        return ServiceConsumer(stack=self, name=consumer_name, shortname=shortname, app_id=app_id)

    def app_logs(self, app_id: str | None = None) -> str:
        """Build + container logs for an app (default: the app under test)."""
        r = self.owner_session.get(f"{self.router_url}/app_logs/{app_id or self._app_id}", timeout=10)
        assert r.status_code == 200, f"app_logs failed: {r.status_code}"
        return r.text
