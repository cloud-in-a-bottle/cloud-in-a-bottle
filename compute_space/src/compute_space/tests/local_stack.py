"""Helpers for running a full local stack: an HTTP-only router on a ``*.localhost`` zone, plus
requests-based setup/deploy flows.

``*.localhost`` resolves to loopback on Linux and macOS without any DNS setup, so this works
in browsers, curl, and tests with no real domain.  Used by tests/test_services_e2e.py and
scripts/run_local_stack.py.

All owner requests must go through the zone domain (not 127.0.0.1) so the session cookie —
scoped to the zone domain — is accepted by the client and sent to app subdomains.
"""

import sqlite3
import subprocess
from contextlib import closing

import attr
import requests

from compute_space.config import Config
from compute_space.config import DefaultConfig
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.domains import Domain
from compute_space.core.domains import primary_domain
from compute_space.core.domains import seed_domains
from compute_space.db.connection import init_db
from compute_space.tests.utils import poll
from compute_space.tests.utils import wait_app_running

OWNER_PASSWORD = "localstackpass123"


def make_local_stack_config(
    data_root_dir: str,
    port: int,
    zone_name: str,
    port_range_start: int = 9000,
    port_range_end: int = 9999,
    default_apps: list[str] | None = None,
    apps_dir_override: str | None = None,
) -> Config:
    """Config for a loopback-only, HTTP-only router suitable for local dev and tests.

    ``default_apps=None`` keeps DefaultConfig's standard set (deployed at /setup completion);
    pass ``[]`` to deploy nothing.  ``apps_dir_override`` points at a vendored-builtins dir
    (e.g. the repo's apps/); None keeps the default under data_root_dir.
    """
    config: Config = DefaultConfig(
        host="127.0.0.1",
        port=port,
        data_root_dir=data_root_dir,
        apps_dir_override=apps_dir_override,
        port_range_start=port_range_start,
        port_range_end=port_range_end,
        start_caddy=False,
        claim_token_required=False,
    )
    if default_apps is not None:
        config = config.evolve(default_apps=default_apps)
    config.make_all_dirs()
    init_db(config.db_path)
    with closing(sqlite3.connect(config.db_path)) as db:
        db.row_factory = sqlite3.Row
        seed_domains(db, Domain(name=f"{zone_name}.localhost:{port}", tls=False), [])
    return config


@attr.s(auto_attribs=True, frozen=True)
class LocalStack:
    config: Config
    owner_password: str = OWNER_PASSWORD
    # app names deployed via deploy_app, so remove_deployed_app_containers can clean up
    deployed_app_names: list[str] = attr.Factory(list)

    def _primary_name(self) -> str:
        with closing(sqlite3.connect(self.config.db_path)) as db:
            db.row_factory = sqlite3.Row
            return primary_domain(db).name

    @property
    def router_url(self) -> str:
        return f"http://{self._primary_name()}"

    def app_url(self, app_name: str) -> str:
        return f"http://{app_name}.{self._primary_name()}"

    def remove_deployed_app_containers(self) -> None:
        """Remove app containers after the router is gone.

        App containers run with ``--restart=unless-stopped`` and are not children of the
        router process, so killing the router leaves them running.  Call this in fixture
        teardown to avoid leaking containers across test runs.
        """
        for app_name in self.deployed_app_names:
            subprocess.run(["podman", "rm", "-f", f"openhost-{app_name}"], capture_output=True, timeout=60)


def complete_setup(stack: LocalStack, timeout: float = 60) -> requests.Session:
    """Provision the owner via /setup and return an authenticated session.

    /setup responds 200 with the session cookie, then restarts the router process
    into the full app — so we poll /dashboard until the full app answers with our
    cookie.
    """
    session = requests.Session()
    r = session.post(
        f"{stack.router_url}/setup",
        data={"password": stack.owner_password, "confirm_password": stack.owner_password},
        timeout=30,
    )
    assert r.status_code == 200, f"/setup failed: {r.status_code}: {r.text[:300]}"
    cookie_names = [c.name for c in session.cookies]
    assert SESSION_COOKIE_NAME in cookie_names, f"setup did not set {SESSION_COOKIE_NAME} cookie, got {cookie_names}"

    def _dashboard_up() -> bool:
        try:
            return session.get(f"{stack.router_url}/dashboard", timeout=2).status_code == 200
        except requests.ConnectionError:
            return False

    poll(_dashboard_up, timeout=timeout, interval=0.5, fail_msg="full app did not come up after /setup")
    return session


def deploy_app(
    session: requests.Session,
    stack: LocalStack,
    repo_url: str,
    app_name: str | None = None,
    grant_manifest_permissions: bool = False,
    timeout: float = 300,
) -> str:
    """Deploy an app via /api/add_app and wait until it is running.  Returns the app_id."""
    payload: dict[str, str | bool] = {"repo_url": repo_url}
    if app_name is not None:
        payload["app_name"] = app_name
    if grant_manifest_permissions:
        payload["grant_permissions_v2"] = True
    r = session.post(f"{stack.router_url}/api/add_app", json=payload, timeout=120)
    assert r.status_code == 200, f"add_app({repo_url}) failed: {r.status_code}: {r.text[:500]}"
    body = r.json()
    stack.deployed_app_names.append(body["app_name"])
    wait_app_running(session, stack.router_url, body["app_name"], timeout=timeout)
    return str(body["app_id"])
