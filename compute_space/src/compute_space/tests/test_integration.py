"""
End-to-end integration tests for the OpenHost router.

The full production flow is: build outer VM -> boot VM -> start router inside
VM -> deploy apps.  The VM step requires Linux with KVM and diskimage-builder,
so these tests run the router directly on the host and exercise the rootless
podman runtime natively.  This covers all the same code paths the router
would use inside the VM.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import requests
from loguru import logger

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.core.caddy import generate_caddyfile
from compute_space.core.data import provision_data
from compute_space.core.domains import Domain
from compute_space.core.manifest import AppManifest
from compute_space.tests.conftest import _make_config_and_env
from compute_space.tests.conftest import _start_router_process
from compute_space.tests.conftest import _stop_router_process
from compute_space.tests.conftest import primary_of
from compute_space.tests.container import container_cleanup
from compute_space.tests.utils import app_id_for
from compute_space.tests.utils import wait_app_removed
from compute_space.tests.utils import wait_app_running

_APPS_DIR = str(OPENHOST_PROJECT_DIR / "apps")

requires_containers = pytest.mark.requires_containers


def test_sqlite_provisioning():
    """SQLite databases declared in a manifest are provisioned correctly."""
    with (
        tempfile.TemporaryDirectory() as data_dir,
        tempfile.TemporaryDirectory() as temp_dir,
        tempfile.TemporaryDirectory() as archive_dir,
    ):
        manifest = AppManifest(
            name="testapp",
            version="0.1.0",
            container_image="alpine:latest",
            container_port=8080,
            sqlite_dbs=["main", "cache"],
        )

        env_vars = provision_data(
            app_id="testapp-id",
            app_name="testapp",
            manifest=manifest,
            data_dir=data_dir,
            temp_data_dir=temp_dir,
            archive_dir=archive_dir,
            my_openhost_redirect_domain="my.test.example.com",
            zone_domain="test.example.com",
            port=manifest.container_port,
            owner_username="owner",
        )

        sqlite_dir = os.path.join(data_dir, "app_data", "testapp", "sqlite")
        assert os.path.isdir(sqlite_dir)

        assert "OPENHOST_SQLITE_main" in env_vars
        assert "OPENHOST_SQLITE_MAIN" in env_vars
        assert "OPENHOST_SQLITE_cache" in env_vars
        assert "OPENHOST_SQLITE_CACHE" in env_vars
        assert env_vars["OPENHOST_SQLITE_main"] == os.path.join(sqlite_dir, "main.db")
        assert env_vars["OPENHOST_SQLITE_MAIN"] == os.path.join(sqlite_dir, "main.db")
        assert env_vars["OPENHOST_SQLITE_cache"] == os.path.join(sqlite_dir, "cache.db")
        assert env_vars["OPENHOST_SQLITE_CACHE"] == os.path.join(sqlite_dir, "cache.db")

        # .db files should NOT exist yet — the app creates them
        assert not os.path.exists(env_vars["OPENHOST_SQLITE_main"])
        assert not os.path.exists(env_vars["OPENHOST_SQLITE_cache"])


def test_pre_setup_health(tmp_path):
    """Health endpoint returns status ok and no security data before owner setup."""
    ROUTER_PORT = 18084
    base_url = f"http://127.0.0.1:{ROUTER_PORT}"

    _config, env = _make_config_and_env(tmp_path, port=ROUTER_PORT)

    router = None
    try:
        router = _start_router_process(base_url, env)

        r = requests.get(f"{base_url}/health")
        assert r.status_code == 200

        data = r.json()
        assert data["status"] == "ok"
        assert "security" not in data
    finally:
        if router is not None:
            _stop_router_process(router)


def test_caddyfile_http_redirect():
    """A TLS domain serves https and redirects its http site to https."""
    cert = Path("/etc/ssl/cert.pem")
    key = Path("/etc/ssl/key.pem")
    caddyfile = generate_caddyfile(
        (Domain("host.example.com", tls=True),),
        8080,
        lambda name: (cert, key) if name == "host.example.com" else None,
    )

    # https site for the domain + its wildcard, using the acquired file cert
    assert "https://host.example.com, https://*.host.example.com {" in caddyfile
    assert "tls /etc/ssl/cert.pem /etc/ssl/key.pem" in caddyfile

    # scoped http site that redirects to https (not a global :80 catch-all)
    assert "http://host.example.com, http://*.host.example.com {" in caddyfile
    assert "redir https://{host}{uri} permanent" in caddyfile

    # the http (redirect) block should NOT reverse_proxy
    redirect_block = caddyfile.split("http://host.example.com")[1].split("}")[0]
    assert "reverse_proxy" not in redirect_block


def _setup_owner(session, base_url, password="testpass123", username=None, timeout=30):
    """POST /setup to provision the owner, then wait for the full app to come up.

    The setup-only Litestar app starts shutting down once the POST returns so
    start.py can boot the full app; subsequent requests would 404 against the
    setup app until that handoff completes.  ``/login`` lives only on the full
    app, so we use it as the "full app is up" probe.  Mirrors the
    ``admin_session`` fixture's pattern in conftest.py.
    """
    data = {"password": password, "confirm_password": password}
    if username is not None:
        data["username"] = username
    r = session.post(f"{base_url}/setup", data=data)
    assert r.status_code == 200, f"Router setup failed: {r.status_code}: {r.text[:300]}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            probe = requests.get(f"{base_url}/login", timeout=1, allow_redirects=False)
            if probe.status_code in (200, 302):
                return r
        except requests.ConnectionError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Full app did not come up within {timeout}s after /setup")


def _deploy_app(session, base_url, app_path, app_name=None, timeout=120):
    """Deploy a local app directory via the file:// URL flow.

    Returns the response from the deploy POST.
    """
    repo_url = f"file://{app_path}"
    data = {"repo_url": repo_url}
    if app_name:
        data["app_name"] = app_name

    r = session.post(f"{base_url}/api/add_app", json=data, timeout=timeout)
    assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:300]}"
    return r


# ---------------------------------------------------------------------------
# Router-only tests (no container runtime needed)
# ---------------------------------------------------------------------------


class TestRouterCore:
    """Tests that only need the router running, no external runtimes."""

    def test_health(self, router_process, config):
        base_url = _zone_url(config)
        r = requests.get(f"{base_url}/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "security" not in data

    def test_dashboard_requires_auth(self, admin_session, config):
        """Unauthenticated requests to /dashboard redirect to /login."""
        base_url = _zone_url(config)
        # Use a fresh session (no cookies) to test auth redirect
        r = requests.get(
            f"{base_url}/dashboard",
            allow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_login_bad_credentials(self, admin_session, config):
        """Bad credentials on /login show error (owner must exist first)."""
        base_url = _zone_url(config)
        r = requests.post(
            f"{base_url}/login",
            data={"username": "wrong", "password": "wrong"},
        )
        assert r.status_code == 200
        assert "Invalid password" in r.text

    def test_dashboard_after_login(self, admin_session, config):
        base_url = _zone_url(config)
        r = admin_session.get(f"{base_url}/dashboard")
        assert r.status_code == 200
        assert "Deployed Apps" in r.text
        # Storage status and SSH toggle live on the System page, not the dashboard.
        for system_only_element in (
            'id="storage-table"',
            'id="ports-table"',
            'id="ssh-btn"',
        ):
            assert system_only_element not in r.text

    def test_system_page_requires_auth(self, admin_session, config):
        """Unauthenticated requests to /system/ redirect to /login."""
        base_url = _zone_url(config)
        r = requests.get(
            f"{base_url}/system/",
            allow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_system_page_after_login(self, admin_session, config):
        base_url = _zone_url(config)
        r = admin_session.get(f"{base_url}/system/")
        assert r.status_code == 200
        assert 'id="ports-table"' in r.text
        assert 'id="storage-table"' in r.text
        assert 'id="cs-logs"' in r.text
        assert 'id="security-table"' not in r.text

    def test_listening_ports_endpoint(self, admin_session, config):
        """GET /api/listening-ports returns classified external-facing ports."""
        base_url = _zone_url(config)
        r = admin_session.get(f"{base_url}/api/listening-ports")
        assert r.status_code == 200
        data = r.json()
        assert "ports" in data
        assert isinstance(data["ports"], list)
        # True on hosts without ``ss`` (e.g. macOS dev machines); the filter still applies.
        assert isinstance(data["enumeration_failed"], bool)
        # Each entry must have the expected shape, and loopback-only listeners are excluded.
        for entry in data["ports"]:
            assert isinstance(entry["port"], int)
            assert isinstance(entry["address"], str)
            assert entry["classification"] in {"secure", "app_range", "allocated", "unexpected"}
            assert isinstance(entry["label"], str)
            host = entry["address"].rsplit(":", 1)[0].strip("[]")
            assert host != "::1" and not host.startswith("127."), entry

    def test_setup_returns_403_if_already_set_up(self, admin_session, config):
        """GET /setup returns 403 when owner already exists."""
        base_url = _zone_url(config)
        r = requests.get(f"{base_url}/setup", allow_redirects=False)
        assert r.status_code == 403
        assert "already been set up" in r.text

    def test_setup_post_returns_403_if_already_set_up(self, admin_session, config):
        """POST /setup returns 403 when owner already exists."""
        base_url = _zone_url(config)
        r = requests.post(
            f"{base_url}/setup",
            data={"password": "newpass", "confirm_password": "newpass"},
            allow_redirects=False,
        )
        assert r.status_code == 403
        assert "already been set up" in r.text

    def test_add_app_page_shows_catalog_callout(self, admin_session, config):
        """The Deploy page points at the app catalog instead of listing builtin apps."""
        base_url = _zone_url(config)
        r = admin_session.get(f"{base_url}/add_app")
        assert r.status_code == 200
        assert "Explore the App Catalog" in r.text
        assert "Deploy from Git URL" in r.text
        assert "Available Built-in Apps" not in r.text
        assert "test_app" not in r.text

    def test_add_app_no_url(self, admin_session, config):
        base_url = _zone_url(config)
        r = admin_session.post(
            f"{base_url}/api/clone_and_get_app_info",
            json={},
        )
        assert r.status_code == 400

    def test_add_app_bad_path(self, admin_session, config):
        base_url = _zone_url(config)
        r = admin_session.post(
            f"{base_url}/api/clone_and_get_app_info",
            json={"repo_url": "file:///nonexistent/path"},
        )
        assert r.status_code == 400
        assert "Local path does not exist" in r.json()["error"]

    def test_add_app_file_url_non_git_accepted(self, admin_session, config):
        """POST with a file:// URL to a non-git dir with openhost.toml succeeds."""
        base_url = _zone_url(config)
        repo_url = f"file://{_FIXTURES_DIR}/test_app"
        r = admin_session.post(
            f"{base_url}/api/clone_and_get_app_info",
            json={"repo_url": repo_url},
        )
        assert r.status_code == 200, f"Unexpected status {r.status_code}"
        data = r.json()
        assert "manifest" in data
        assert data["app_name"] == "test-app"

    def test_add_app_file_url_git_dir_manifest(self, admin_session, config, tmp_path):
        """POST with a file:// URL to a git-init'd dir fetches openhost.toml."""
        base_url = _zone_url(config)
        git_dir = tmp_path / "test_repo"
        git_dir.mkdir()
        toml_path = git_dir / "openhost.toml"
        toml_path.write_text(
            '[app]\nname = "test-git-dir"\nversion = "0.1.0"\n\n'
            '[runtime.container]\nimage = "Dockerfile"\nport = 5000\n'
        )
        subprocess.run(["git", "init", str(git_dir)], check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(git_dir), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(git_dir),
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        repo_url = f"file://{git_dir}"
        r = admin_session.post(
            f"{base_url}/api/clone_and_get_app_info",
            json={"repo_url": repo_url},
        )
        assert r.status_code == 200, f"Unexpected status {r.status_code}"
        data = r.json()
        assert data["app_name"] == "test-git-dir"

    def test_add_app_file_url_bare_repo_manifest(self, admin_session, config, tmp_path):
        """POST with a file:// URL to a bare git repo fetches openhost.toml."""
        base_url = _zone_url(config)
        bare_path = str(tmp_path / "bare_repo.git")
        _create_bare_git_repo(os.path.join(_FIXTURES_DIR, "test_app"), bare_path)
        repo_url = f"file://{bare_path}"
        r = admin_session.post(
            f"{base_url}/api/clone_and_get_app_info",
            json={"repo_url": repo_url},
        )
        assert r.status_code == 200, f"Unexpected status {r.status_code}"
        data = r.json()
        assert data["app_name"] == "test-app"

    def test_catch_all_404(self, router_process, config):
        """Requests to unknown paths return 404."""
        base_url = _zone_url(config)
        r = requests.get(f"{base_url}/no-such-app/anything")
        assert r.status_code == 404

    def test_api_token_create_and_use(self, admin_session, config):
        """Create an API token, then use it to access a protected endpoint."""
        base = _zone_url(config)

        # Create a token
        r = admin_session.post(f"{base}/api/tokens", json={"name": "test-token", "expiry_hours": "1"})
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert data["name"] == "test-token"
        raw_token = data["token"]

        # Use the token (no cookies) to hit a protected endpoint
        r = requests.get(
            f"{base}/api/apps",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert r.status_code == 200
        # /api/apps returns a list of {app_id, name, status, error_message}.
        assert isinstance(r.json(), list)

        # Verify the token appears in the list
        r = admin_session.get(f"{base}/api/tokens")
        tokens = r.json()
        assert any(t["name"] == "test-token" for t in tokens)

        # Delete the token
        token_id = next(t["id"] for t in tokens if t["name"] == "test-token")
        r = admin_session.delete(f"{base}/api/tokens/{token_id}")
        assert r.status_code == 204

        # Token should no longer work
        r = requests.get(
            f"{base}/api/apps",
            headers={"Authorization": f"Bearer {raw_token}"},
            allow_redirects=False,
        )
        assert r.status_code == 302  # redirects to login

    def test_api_token_no_expiry(self, admin_session, config):
        """Tokens created with expiry_hours=never should work."""
        base = _zone_url(config)
        r = admin_session.post(f"{base}/api/tokens", json={"name": "no-expiry", "expiry_hours": "never"})
        data = r.json()
        assert data["expires_at"] is None

        r = requests.get(
            f"{base}/api/apps",
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r.status_code == 200

        # Clean up
        tokens = admin_session.get(f"{base}/api/tokens").json()
        token_id = next(t["id"] for t in tokens if t["name"] == "no-expiry")
        admin_session.delete(f"{base}/api/tokens/{token_id}")

    def test_api_token_invalid_rejected(self, router_process, config):
        """A bogus Bearer token is rejected."""
        base_url = _zone_url(config)
        r = requests.get(
            f"{base_url}/api/apps",
            headers={"Authorization": "Bearer bogus-token-value"},
            allow_redirects=False,
        )
        assert r.status_code == 302  # redirects to login


# ---------------------------------------------------------------------------
# Claim token setup tests (isolated router, no container runtime needed)
# ---------------------------------------------------------------------------


def test_claim_token_gate_and_delete(tmp_path):
    """With claim_token_required=True, /setup rejects bad/missing tokens, accepts the right
    one, and deletes the on-disk token after a successful claim."""
    ROUTER_PORT = 18083
    base_url = f"http://127.0.0.1:{ROUTER_PORT}"

    config, env = _make_config_and_env(tmp_path, port=ROUTER_PORT, claim_token_required=True)

    claim_token = "test-claim-token-abc123"
    claim_token_path = config.claim_token_path
    with open(claim_token_path, "w") as f:
        f.write(claim_token)

    router = None
    try:
        router = _start_router_process(base_url, env)

        # No token → 403
        r = requests.post(
            f"{base_url}/setup",
            data={"password": "testpass123", "confirm_password": "testpass123"},
            allow_redirects=False,
        )
        assert r.status_code == 403, f"Expected 403 without token, got {r.status_code}"

        # Wrong token → 403
        r = requests.post(
            f"{base_url}/setup",
            params={"claim": "nope"},
            data={"password": "testpass123", "confirm_password": "testpass123", "claim": "nope"},
            allow_redirects=False,
        )
        assert r.status_code == 403, f"Expected 403 with wrong token, got {r.status_code}"

        # Correct token → 200/302; file deleted afterwards
        r = requests.post(
            f"{base_url}/setup",
            params={"claim": claim_token},
            data={
                "password": "testpass123",
                "confirm_password": "testpass123",
                "claim": claim_token,
            },
            allow_redirects=False,
        )
        assert r.status_code in (200, 302), f"Setup failed: {r.status_code} {r.text[:200]}"
        assert not os.path.isfile(claim_token_path), "Claim token file should be deleted after setup"
    finally:
        if router is not None:
            _stop_router_process(router)


def test_setup_open_when_claim_token_not_required(tmp_path):
    """With claim_token_required=False, /setup accepts a caller with no token (legacy/local
    dev behavior)."""
    ROUTER_PORT = 18084
    base_url = f"http://127.0.0.1:{ROUTER_PORT}"

    _config, env = _make_config_and_env(tmp_path, port=ROUTER_PORT, claim_token_required=False)

    router = None
    try:
        router = _start_router_process(base_url, env)
        r = requests.post(
            f"{base_url}/setup",
            data={"password": "testpass123", "confirm_password": "testpass123"},
            allow_redirects=False,
        )
        assert r.status_code in (200, 302), f"Expected open setup to succeed, got {r.status_code}"
    finally:
        if router is not None:
            _stop_router_process(router)


# ---------------------------------------------------------------------------
# Full lifecycle: deploy app (podman), proxy, interact, remove
# ---------------------------------------------------------------------------

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _wait_for_url(session, url, timeout=30, expect_status=200):
    """Poll a URL until it returns the expected status code."""
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            r = session.get(url, timeout=2)
            last_status = r.status_code
            if r.status_code == expect_status:
                return r
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            # ChunkedEncodingError: upstream closed mid-response. Treat as transient
            # and retry — the app may still be warming up just after status='running'.
            pass
        time.sleep(1)
    raise AssertionError(f"URL {url} did not return {expect_status} within {timeout}s (last status: {last_status})")


def _zone_url(config):
    """Zone base URL — resolves to 127.0.0.1 via the DNS fixture in conftest.py."""
    return f"http://{primary_of(config).name}:{config.port}"


def _app_url(config, app_name):
    """App subdomain base URL — same DNS trick applies."""
    return f"http://{app_name}.{primary_of(config).name}:{config.port}"


@requires_containers
class TestContainerGone:
    """Test that apps recover when their container is completely gone.

    Simulates a real VM-reboot scenario where the container engine's local
    storage has been wiped (e.g. the data-root disk didn't remount at boot).
    check_app_status() should detect this and do a full rebuild
    (image + container) instead of failing with "No such container".
    """

    APP_PATH = os.path.join(_FIXTURES_DIR, "test_app")
    APP_NAME = "test-app"
    CONTAINER_NAME = "openhost-test-app"
    ROUTER_PORT = 18082
    BASE_URL = "http://testzone.local:18082"

    def test_app_recovers_after_container_removed(self, tmp_path):
        """After container is completely removed, router should rebuild and restart.

        Exercises the real VM-reboot scenario:
        1. Deploy a app — running and healthy.
        2. Stop the router process.
        3. Remove the container AND image (simulates the engine's state
           being lost because data-root wasn't mounted at boot).
        4. Start the router — check_app_status() detects the dead container,
           does a full rebuild via _start_app_process().
        5. Verify a new container is running and serving traffic.
        6. Assert the router's DB shows status='running' with a new container ID.
        """
        config, env = _make_config_and_env(tmp_path, port=self.ROUTER_PORT)
        db_path = config.db_path
        router = None

        container_cleanup(self.CONTAINER_NAME, self.APP_NAME)

        try:
            # ---- Phase 1: Deploy and verify the app is running ----
            router = _start_router_process(self.BASE_URL, env)

            session = requests.Session()
            _setup_owner(session, self.BASE_URL, username="admin")

            _deploy_app(session, self.BASE_URL, self.APP_PATH)

            # Wait for running status in DB.
            # The first deploy builds the image from scratch (no cache),
            # which can take well over 15 s in CI.  Use the same generous
            # timeout that TestContainerE2E.test_app_detail uses (120 s).
            deadline = time.time() + 120
            db_status = None
            db_error = None
            while time.time() < deadline:
                try:
                    poll_db = sqlite3.connect(db_path)
                    try:
                        poll_db.row_factory = sqlite3.Row
                        poll_row = poll_db.execute(
                            "SELECT status, error_message FROM apps WHERE name = ?",
                            (self.APP_NAME,),
                        ).fetchone()
                        if poll_row:
                            db_status = poll_row["status"]
                            db_error = poll_row["error_message"]
                    finally:
                        poll_db.close()
                except Exception as e:
                    logger.error(f"Error polling DB for app status: {e}")
                    pass
                logger.info(f"Polled DB for app status: {db_status}")
                # 'error' is terminal — stop waiting out the full deadline and
                # surface the failure reason instead of a bare status.
                if db_status in ("running", "error"):
                    break
                time.sleep(2)
            assert db_status == "running", (
                f"App should be running after deploy, got status={db_status} (error_message={db_error!r})"
            )

            # Record old container ID
            db = sqlite3.connect(db_path)
            try:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT container_id FROM apps WHERE name = ?",
                    (self.APP_NAME,),
                ).fetchone()
                old_container_id = row["container_id"]
            finally:
                db.close()
            assert old_container_id, "Should have a container ID after deploy"

            # ---- Phase 2: Simulate container completely gone ----
            _stop_router_process(router)
            router = None

            # Remove container AND image — simulates engine state loss
            subprocess.run(
                ["podman", "rm", "-f", self.CONTAINER_NAME],
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ["podman", "rmi", "-f", f"openhost-{self.APP_NAME}:latest"],
                capture_output=True,
                timeout=30,
            )

            # Verify container is truly gone
            result = subprocess.run(
                ["podman", "inspect", self.CONTAINER_NAME],
                capture_output=True,
                timeout=10,
            )
            assert result.returncode != 0, "Container should not exist"

            # ---- Phase 3: Restart router (triggers check_app_status) ----
            # check_app_status() does a synchronous image rebuild during
            # startup, so /health won't respond until the rebuild finishes.
            # Give it enough time for the full image build + container start.
            router = _start_router_process(self.BASE_URL, env, startup_timeout=180)

            # ---- Phase 4: Verify full rebuild happened ----
            # check_app_status() should have rebuilt the image and created
            # a new container via _start_app_process()
            deadline = time.time() + 15  # router is already up, just verify DB
            db_status = None
            db_error = None
            new_container_id = None
            while time.time() < deadline:
                try:
                    poll_db = sqlite3.connect(db_path)
                    try:
                        poll_db.row_factory = sqlite3.Row
                        poll_row = poll_db.execute(
                            "SELECT status, error_message, container_id FROM apps WHERE name = ?",
                            (self.APP_NAME,),
                        ).fetchone()
                        if poll_row:
                            db_status = poll_row["status"]
                            db_error = poll_row["error_message"]
                            new_container_id = poll_row["container_id"]
                    finally:
                        poll_db.close()
                except Exception:
                    pass
                if db_status in ("running", "error"):
                    break
                time.sleep(2)

            assert db_status == "running", (
                f"App should be running after rebuild, got status={db_status} (error_message={db_error!r})"
            )
            assert new_container_id, "Should have a new container ID"
            assert new_container_id != old_container_id, "Container ID should be different after rebuild"

            # Verify the new container is actually serving traffic
            deadline = time.time() + 15
            healthy = False
            while time.time() < deadline:
                try:
                    poll_db = sqlite3.connect(db_path)
                    try:
                        poll_db.row_factory = sqlite3.Row
                        poll_row = poll_db.execute(
                            "SELECT local_port FROM apps WHERE name = ?",
                            (self.APP_NAME,),
                        ).fetchone()
                    finally:
                        poll_db.close()
                    if poll_row:
                        r = requests.get(
                            f"http://127.0.0.1:{poll_row['local_port']}/health",
                            timeout=2,
                        )
                        if r.status_code == 200:
                            healthy = True
                            break
                except Exception:
                    pass
                time.sleep(1)
            assert healthy, "Rebuilt container should be healthy and serving traffic"

        finally:
            if router:
                try:
                    s = requests.Session()
                    s.post(
                        f"{self.BASE_URL}/login",
                        data={"username": "admin", "password": "testpass123"},
                    )
                    app_id = app_id_for(s, self.BASE_URL, self.APP_NAME)
                    if app_id:
                        s.post(
                            f"{self.BASE_URL}/remove_app/{app_id}",
                            timeout=10,
                        )
                    # Wait for the bg worker before stopping the router
                    # or it gets killed mid-teardown. container_cleanup()
                    # below force-removes anything left over.
                    wait_app_removed(s, self.BASE_URL, self.APP_NAME, timeout=30)
                except Exception:
                    pass
                _stop_router_process(router)
            container_cleanup(self.CONTAINER_NAME, self.APP_NAME)


@requires_containers
class TestContainerRestart:
    """Test that apps recover after the container engine restarts.

    Simulates the VM reboot path: stop the container, then restart the
    router.  check_app_status() detects the dead container and does a full
    rebuild (image + new container) via _start_app_process().
    """

    APP_PATH = os.path.join(_FIXTURES_DIR, "test_app")
    APP_NAME = "test-app"
    CONTAINER_NAME = "openhost-test-app"
    ROUTER_PORT = 18081
    BASE_URL = "http://testzone.local:18081"

    def test_app_status_after_container_restart(self, tmp_path):
        """After the engine restarts, router should rebuild and show app as 'running'.

        Exercises the engine-restart scenario:
        1. Deploy an app — running and healthy.
        2. Stop the router process.
        3. Stop the container (simulates engine shutdown).
        4. Start the router — check_app_status() detects the dead
           container and does a full rebuild via _start_app_process().
        5. Verify a container is running and serving traffic.
        6. Assert the router's DB shows status='running'.
        """
        config, env = _make_config_and_env(tmp_path, port=self.ROUTER_PORT)
        db_path = config.db_path
        router = None

        # Clean up any leftover containers from previous runs
        container_cleanup(self.CONTAINER_NAME, self.APP_NAME)

        try:
            # ---- Phase 1: Deploy and verify the app is running ----
            router = _start_router_process(self.BASE_URL, env)

            session = requests.Session()
            _setup_owner(session, self.BASE_URL, username="admin")

            # Submit and confirm deployment
            _deploy_app(session, self.BASE_URL, self.APP_PATH)

            # Wait for app status='running' in the DB (not just the HTML).
            # The initial image build can be slow in CI without cache, so
            # use a generous timeout consistent with TestContainerE2E (120 s).
            deadline = time.time() + 120
            db_status = None
            while time.time() < deadline:
                try:
                    poll_db = sqlite3.connect(db_path)
                    try:
                        poll_db.row_factory = sqlite3.Row
                        poll_row = poll_db.execute(
                            "SELECT status FROM apps WHERE name = ?",
                            (self.APP_NAME,),
                        ).fetchone()
                        if poll_row:
                            db_status = poll_row["status"]
                    finally:
                        poll_db.close()
                except Exception:
                    pass
                if db_status == "running":
                    break
                time.sleep(2)
            assert db_status == "running", f"App should be running after deploy, got status={db_status}"

            # Verify proxy works
            _wait_for_url(
                session,
                f"http://{self.APP_NAME}.{primary_of(config).name}:{self.ROUTER_PORT}/health",
                timeout=30,
            )

            # ---- Phase 2: Simulate container engine restart ----
            #
            # In a real VM reboot the container engine stops (killing all
            # containers), then restarts.  We simulate this by stopping
            # the router and the container, then restarting the router
            # so that check_app_status() sees the exited container.

            _stop_router_process(router)
            router = None

            # Stop the container (simulates the container engine shutting it down)
            result = subprocess.run(
                ["podman", "stop", self.CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, f"podman stop failed: {result.stderr}"

            # Verify container is stopped
            result = subprocess.run(
                [
                    "podman",
                    "inspect",
                    "--format",
                    "{{.State.Status}}",
                    self.CONTAINER_NAME,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.stdout.strip() == "exited", "Container should be exited after podman stop"

            # ---- Phase 3: Restart router (triggers check_app_status) ----
            #
            # check_app_status() detects the stopped container and does a
            # full rebuild (image + new container).  This blocks startup,
            # so allow extra time for /health to respond.
            router = _start_router_process(self.BASE_URL, env, startup_timeout=180)

            # ---- Phase 4: Verify check_app_status() restarted the container ----
            #
            # check_app_status() should have already started the container
            # during Phase 3.  Verify it is running *before* any additional
            # podman start call -- this ensures the router's init logic
            # (not a later manual start) is what brought the container back.
            deadline = time.time() + 15
            container_running = False
            while time.time() < deadline:
                result = subprocess.run(
                    [
                        "podman",
                        "inspect",
                        "--format",
                        "{{.State.Status}}",
                        self.CONTAINER_NAME,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.stdout.strip() == "running":
                    container_running = True
                    break
                time.sleep(1)
            assert container_running, "check_app_status() should have restarted the container"

            # Verify the container is actually serving traffic
            deadline = time.time() + 15
            container_healthy = False
            last_poll_error = None
            while time.time() < deadline:
                try:
                    # Hit the container directly on its mapped port
                    db = sqlite3.connect(db_path)
                    try:
                        db.row_factory = sqlite3.Row
                        row = db.execute(
                            "SELECT local_port FROM apps WHERE name = ?",
                            (self.APP_NAME,),
                        ).fetchone()
                    finally:
                        db.close()
                    if row:
                        r = requests.get(
                            f"http://127.0.0.1:{row['local_port']}/health",
                            timeout=2,
                        )
                        if r.status_code == 200:
                            container_healthy = True
                            break
                except Exception as exc:
                    last_poll_error = exc
                time.sleep(1)
            assert container_healthy, (
                f"Container should be healthy and serving traffic (last poll error: {last_poll_error})"
            )

            # ---- Phase 5: Assert router status matches reality ----
            #
            # The container IS running and healthy.  The router's
            # background thread (_restart_apps_sequential) may still be
            # finishing _wait_for_ready() before it commits
            # status='running' to the DB, so poll instead of reading once.

            session = requests.Session()
            r = session.post(
                f"{self.BASE_URL}/login",
                data={"username": "admin", "password": "testpass123"},
            )

            app_id = app_id_for(session, self.BASE_URL, self.APP_NAME)
            assert app_id is not None, "App row should exist after rebuild"
            r = session.get(f"{self.BASE_URL}/app_detail/{self.APP_NAME}")
            assert r.status_code == 200

            # Poll the DB for status='running' (background thread may
            # still be finishing the _wait_for_ready() health check).
            deadline = time.time() + 30
            db_status = None
            while time.time() < deadline:
                try:
                    poll_db = sqlite3.connect(db_path)
                    try:
                        poll_db.row_factory = sqlite3.Row
                        poll_row = poll_db.execute(
                            "SELECT status FROM apps WHERE name = ?",
                            (self.APP_NAME,),
                        ).fetchone()
                        if poll_row:
                            db_status = poll_row["status"]
                    finally:
                        poll_db.close()
                except Exception:
                    pass
                if db_status == "running":
                    break
                time.sleep(2)

            assert db_status == "running", (
                f"App status in DB is '{db_status}' but the container "
                f"is running and healthy.  check_app_status() should have "
                f"restarted the container and set status to 'running'."
            )

        finally:
            if router:
                # Try to remove through the router
                try:
                    s = requests.Session()
                    s.post(
                        f"{self.BASE_URL}/login",
                        data={
                            "username": "admin",
                            "password": "testpass123",
                        },
                    )
                    app_id = app_id_for(s, self.BASE_URL, self.APP_NAME)
                    if app_id:
                        s.post(
                            f"{self.BASE_URL}/remove_app/{app_id}",
                            timeout=10,
                        )
                    # Wait for the bg worker before stopping the router.
                    wait_app_removed(s, self.BASE_URL, self.APP_NAME, timeout=30)
                except Exception:
                    pass
                _stop_router_process(router)
            container_cleanup(self.CONTAINER_NAME, self.APP_NAME)


@requires_containers
class TestContainerE2E:
    """
    End-to-end test of the container deployment path using a minimal test app.

    Tests run in definition order within the class.  Each test builds on the
    state left by the previous one (deploy -> interact -> remove).
    """

    APP_PATH = os.path.join(_FIXTURES_DIR, "test_app")

    # -- deploy --

    def test_deploy(self, admin_session, config):
        """Deploy the test app via the router dashboard."""
        base_url = _zone_url(config)
        r = _deploy_app(admin_session, base_url, self.APP_PATH)
        assert "test-app" in r.text

    def test_app_detail(self, admin_session, config):
        """The app detail page shows correct metadata once build completes."""
        base_url = _zone_url(config)
        # Wait for background deploy to finish
        deadline = time.time() + 120
        r = None
        while time.time() < deadline:
            app_id = app_id_for(admin_session, base_url, "test-app")
            if app_id:
                r = admin_session.get(f"{base_url}/app_detail/test-app")
                if r.status_code == 200 and "running" in r.text:
                    break
            time.sleep(2)
        assert r is not None and r.status_code == 200
        assert "running" in r.text
        assert "/test-app" in r.text

    # -- proxy: health --

    def test_proxy_health(self, admin_session, config, router_process):
        """Wait for the app to become ready through the reverse proxy."""
        url = f"{_app_url(config, 'test-app')}/health"
        deadline = time.time() + 120
        last_status = None
        last_err = None
        while time.time() < deadline:
            try:
                r = admin_session.get(url, timeout=2)
                last_status = r.status_code
                if r.status_code == 200:
                    assert r.json() == {"status": "ok"}
                    return
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
            time.sleep(1)
        pytest.fail(f"App did not become ready within timeout (last_status={last_status}, last_err={last_err})")

    # -- proxy: interact --

    def test_proxy_get(self, admin_session, config):
        """GET request is proxied via subdomain routing."""
        r = admin_session.get(f"{_app_url(config, 'test-app')}/")
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "test-app"
        assert data["app_name"] == "test-app"

    def test_proxy_post(self, admin_session, config):
        """POST request is proxied correctly with body."""
        r = admin_session.post(
            f"{_app_url(config, 'test-app')}/submit",
            data="hello world",
        )
        assert r.status_code == 200
        data = r.json()
        assert data["method"] == "POST"
        assert data["body"] == "hello world"
        assert data["path"] == "/submit"

    def test_proxy_forwards_headers(self, admin_session, config):
        """Proxy forwards custom headers and adds X-Forwarded-* headers."""
        r = admin_session.get(
            f"{_app_url(config, 'test-app')}/echo-headers",
            headers={"X-Custom-Test": "test-value"},
        )
        assert r.status_code == 200
        # ASGI normalises incoming header names to lowercase, so compare CI
        # rather than depending on the case the backend happens to see.
        headers_ci = {k.lower(): v for k, v in r.json()["headers"].items()}
        assert headers_ci.get("x-custom-test") == "test-value"
        assert "x-forwarded-for" in headers_ci
        assert "x-forwarded-host" in headers_ci
        assert headers_ci.get("x-forwarded-proto") == "http"  # tls_enabled is False in tests
        # Host is preserved as the public hostname, not the internal 127.0.0.1:<port>,
        # so apps that only read Host (Django ALLOWED_HOSTS, absolute-URL builders) work.
        assert headers_ci.get("host") == f"test-app.{primary_of(config).name}:{config.port}"

    def test_proxy_forwarded_headers_trust_model(self, admin_session, config):
        """X-Forwarded-Proto/Host are derived by the router, never taken from the
        client.  X-Forwarded-For is trusted only from the loopback front proxy —
        which the test client is — so its value is passed through to the app."""
        r = admin_session.get(
            f"{_app_url(config, 'test-app')}/echo-headers",
            headers={
                "X-Forwarded-For": "203.0.113.7",
                "X-Forwarded-Proto": "evil",
                "X-Forwarded-Host": "evil.example.com",
            },
        )
        assert r.status_code == 200
        headers = {k.lower(): v for k, v in r.json()["headers"].items()}
        # proto/host come from the router (config + Host), not the client
        assert headers.get("x-forwarded-proto") == "http"  # tls_enabled is False in tests
        assert headers.get("x-forwarded-host") != "evil.example.com"
        # the caller reaches us over loopback, so it's the trusted front proxy:
        # its X-Forwarded-For (the real client IP) is honored.
        assert headers.get("x-forwarded-for") == "203.0.113.7"

    def test_proxy_strips_zone_auth_cookies(self, admin_session, config):
        """The owner's zone_auth / zone_refresh cookies must not reach the backend app."""
        r = admin_session.get(f"{_app_url(config, 'test-app')}/echo-headers")
        assert r.status_code == 200
        cookie_header = r.json()["headers"].get("Cookie", "")
        assert "zone_auth=" not in cookie_header
        assert "zone_refresh=" not in cookie_header

    def test_proxy_strips_spoofed_openhost_headers(self, admin_session, config):
        """Client-supplied X-OpenHost-* headers must be stripped — only the router may set them."""
        r = admin_session.get(
            f"{_app_url(config, 'test-app')}/echo-headers",
            headers={
                "X-OpenHost-Is-Owner": "true",
                "X-OpenHost-Identity": "spoofed",
            },
        )
        assert r.status_code == 200
        headers = r.json()["headers"]
        # The router strips inbound X-OpenHost-* and re-injects its own.
        # X-OpenHost-Is-Owner may be re-set by the router, but the spoofed
        # X-OpenHost-Identity value must not be passed through.
        assert headers.get("X-OpenHost-Identity") != "spoofed"

    def test_proxy_404(self, admin_session, config):
        """Unknown paths within the app return the app's 404."""
        r = admin_session.get(f"{_app_url(config, 'test-app')}/no-such-path")
        assert r.status_code == 404

    # -- stop / reload --

    def test_stop(self, admin_session, config):
        """Stop the app — container is killed, proxied requests fail."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, "test-app")
        r = admin_session.post(
            f"{base_url}/stop_app/{app_id}",
        )
        assert r.status_code == 200

        # Proxied requests should now fail
        r = admin_session.get(
            f"{_app_url(config, 'test-app')}/health",
            timeout=2,
        )
        assert r.status_code in (404, 502)

    def test_reload(self, admin_session, config):
        """Reload the app — rebuilds image, restarts container."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, "test-app")
        r = admin_session.post(
            f"{base_url}/reload_app/{app_id}",
            timeout=120,
        )
        assert r.status_code == 200

        # Wait for it to come back (the rebuild may take a while under load)
        # Also poll the API for status to detect errors early.
        url = f"{_app_url(config, 'test-app')}/health"
        status_url = f"{base_url}/api/app_status/{app_id}"
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                r = admin_session.get(url, timeout=2)
                if r.status_code == 200:
                    return
            except (requests.ConnectionError, requests.Timeout):
                pass
            # Check if the reload errored out
            try:
                sr = admin_session.get(status_url, timeout=2)
                if sr.status_code == 200:
                    status_data = sr.json()
                    if status_data.get("status") == "error":
                        pytest.fail(f"App reload failed: {status_data.get('error')}")
            except Exception:
                pass
            time.sleep(1)
        # Grab final status for the failure message
        try:
            sr = admin_session.get(status_url, timeout=2)
            status_info = sr.json() if sr.status_code == 200 else {}
        except Exception:
            status_info = {}
        pytest.fail(f"App did not come back after reload. Status: {status_info}")

    # -- remove --

    def test_remove(self, admin_session, config):
        """Remove the app — stops container, cleans data."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, "test-app")
        r = admin_session.post(
            f"{base_url}/remove_app/{app_id}",
        )
        assert r.status_code == 202
        wait_app_removed(admin_session, base_url, "test-app")

    def test_proxy_gone_after_remove(self, admin_session, config):
        """After removal, proxied requests should 404."""
        r = admin_session.get(
            f"{_app_url(config, 'test-app')}/health",
            timeout=2,
        )
        assert r.status_code == 404

    def test_data_cleaned_up(self, config):
        """App data directory should be deleted after removal."""
        app_data = os.path.join(config.persistent_data_dir, "app_data", "test-app")
        app_temp = os.path.join(config.temporary_data_dir, "app_temp_data", "test-app")
        assert not os.path.exists(app_data)
        assert not os.path.exists(app_temp)

    def test_container_cleaned_up(self):
        """Container should be removed after app removal."""
        r = subprocess.run(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                "name=openhost-test-app",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "openhost-test-app" not in r.stdout


# ---------------------------------------------------------------------------
# Remove app with keep_data: deploy, write data, remove (keep), re-deploy,
# verify data survived
# ---------------------------------------------------------------------------


@requires_containers
class TestRemoveKeepData:
    """
    Test that 'Remove App (Keep Data)' preserves persistent app data so the
    app can be re-deployed and pick up where it left off.

    Uses the test app fixture. Tests run in definition order.
    """

    APP_PATH = os.path.join(_FIXTURES_DIR, "test_app")

    def test_deploy(self, admin_session, config):
        """Deploy the test app."""
        base_url = _zone_url(config)
        _deploy_app(admin_session, base_url, self.APP_PATH)
        wait_app_running(admin_session, base_url, "test-app", timeout=120)

    def test_create_data_file(self, config):
        """Write a marker file into the app's persistent data directory."""
        app_data = os.path.join(config.persistent_data_dir, "app_data", "test-app")
        os.makedirs(app_data, exist_ok=True)
        marker = os.path.join(app_data, "keep_data_test.txt")
        with open(marker, "w") as f:
            f.write("preserve-me")
        assert os.path.isfile(marker)

    def test_remove_keep_data(self, admin_session, config):
        """Remove app with keep_data=1, persistent data survives."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, "test-app")
        r = admin_session.post(
            f"{base_url}/remove_app/{app_id}",
            json={"keep_data": True},
        )
        assert r.status_code == 202
        wait_app_removed(admin_session, base_url, "test-app")

        # Persistent data should still exist
        marker = os.path.join(
            config.persistent_data_dir,
            "app_data",
            "test-app",
            "keep_data_test.txt",
        )
        assert os.path.isfile(marker), "Persistent data should survive remove with keep_data"

        # Temp data should be cleaned up
        app_temp = os.path.join(config.temporary_data_dir, "app_temp_data", "test-app")
        assert not os.path.exists(app_temp), "Temp data should be removed"

    def test_redeploy_picks_up_data(self, admin_session, config):
        """Re-deploy the same app; persistent data is still there."""
        base_url = _zone_url(config)
        _deploy_app(admin_session, base_url, self.APP_PATH)
        wait_app_running(admin_session, base_url, "test-app", timeout=120)

        # Marker file from before removal should still be on disk
        marker = os.path.join(
            config.persistent_data_dir,
            "app_data",
            "test-app",
            "keep_data_test.txt",
        )
        assert os.path.isfile(marker), "Data should persist across remove+redeploy"
        with open(marker) as f:
            assert f.read() == "preserve-me"

    def test_cleanup(self, admin_session, config):
        """Final cleanup: remove app fully (with data)."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, "test-app")
        r = admin_session.post(
            f"{base_url}/remove_app/{app_id}",
        )
        assert r.status_code == 202
        wait_app_removed(admin_session, base_url, "test-app")

        app_data = os.path.join(config.persistent_data_dir, "app_data", "test-app")
        app_temp = os.path.join(config.temporary_data_dir, "app_temp_data", "test-app")
        assert not os.path.exists(app_data), "Full remove should delete persistent data"
        assert not os.path.exists(app_temp), "Full remove should delete temp data"


# ---------------------------------------------------------------------------
# Full lifecycle: deploy from Git URL (remote repo flow), proxy, reload, remove
# ---------------------------------------------------------------------------


def _create_bare_git_repo(source_dir, bare_repo_path):
    """Create a bare git repo from a source directory.

    Initialises a bare repo, commits all files from source_dir, and pushes
    to the bare repo so it can be cloned via file:// URL.
    """
    # Create bare repo
    subprocess.run(
        ["git", "init", "--bare", bare_repo_path],
        check=True,
        capture_output=True,
    )
    # Point HEAD to main so 'git show HEAD:...' works after pushing to main
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=bare_repo_path,
        check=True,
        capture_output=True,
    )

    # Create a temporary working copy, commit, and push
    work_dir = bare_repo_path + "_work"
    try:
        shutil.copytree(source_dir, work_dir)
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "test"
        env["GIT_AUTHOR_EMAIL"] = "test@test"
        env["GIT_COMMITTER_NAME"] = "test"
        env["GIT_COMMITTER_EMAIL"] = "test@test"
        subprocess.run(["git", "init"], cwd=work_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "branch", "-m", "main"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=work_dir,
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", bare_repo_path],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=work_dir,
            env=env,
            check=True,
            capture_output=True,
        )
    finally:
        # Clean up the working copy even if git commands fail
        shutil.rmtree(work_dir, ignore_errors=True)
    return bare_repo_path


@requires_containers
class TestGitUrlDeployE2E:
    """
    End-to-end test of deploying an app from a Git URL.

    This exercises the remote-repo code path (git clone, not shutil.copytree)
    which is used when deploying from GitHub URLs.  A local bare git repo
    stands in for GitHub to avoid external network dependencies.

    Tests run in definition order within the class.  Each test builds on the
    state left by the previous one (deploy -> proxy -> reload -> remove).
    """

    APP_NAME = "test-git-deploy"
    APP_PATH = os.path.join(_FIXTURES_DIR, "test_app")

    def _repo_url(self, config):
        """Return the file:// URL for the bare git repo."""
        bare_path = os.path.join(config.temporary_data_dir, "test-git-deploy.git")
        return f"file://{bare_path}"

    # -- setup bare repo + deploy --

    def test_deploy_from_git_url(self, admin_session, config):
        """Deploy an app from a file:// Git URL via the API."""
        base_url = _zone_url(config)
        # Create bare git repo from the test fixture
        bare_path = os.path.join(config.temporary_data_dir, "test-git-deploy.git")
        _create_bare_git_repo(self.APP_PATH, bare_path)
        repo_url = self._repo_url(config)

        # Step 1: clone and get app info
        r = admin_session.post(
            f"{base_url}/api/clone_and_get_app_info",
            json={"repo_url": repo_url},
        )
        assert r.status_code == 200, f"clone_and_get_app_info failed: {r.status_code}"
        data = r.json()
        assert "manifest" in data
        clone_dir = data["clone_dir"]

        # Step 2: deploy
        r = admin_session.post(
            f"{base_url}/api/add_app",
            json={
                "repo_url": repo_url,
                "app_name": self.APP_NAME,
                "clone_dir": clone_dir,
            },
            timeout=120,
        )
        assert r.status_code == 200
        assert r.json().get("app_name") == self.APP_NAME

    def test_app_detail_running(self, admin_session, config):
        """Wait for the Git-cloned app to finish building and reach running status.

        Polls the ``/api/app_status`` DB status rather than substring-matching
        the ``/app_detail`` HTML: that page inlines a ``.status-running`` CSS
        rule via layout.html, so ``"running" in r.text`` is true regardless of
        the app's actual status and would let this gate pass while the app is
        still 'building'/'starting'.  A settled 'running' status is a genuine
        precondition for the later reload test, which is refused with 409 while
        the app sits in a transient state.
        """
        base_url = _zone_url(config)
        deadline = time.time() + 120
        status = None
        while time.time() < deadline:
            app_id = app_id_for(admin_session, base_url, self.APP_NAME)
            if app_id:
                sr = admin_session.get(f"{base_url}/api/app_status/{app_id}")
                if sr.status_code == 200:
                    status = sr.json().get("status")
                    if status == "running":
                        break
                    if status == "error":
                        pytest.fail(f"Git-deployed app entered error state: {sr.json().get('error')}")
            time.sleep(2)
        assert status == "running", f"Git-deployed app did not reach running status (last status={status})"

        # The app_detail page should also render and report the running status.
        r = admin_session.get(f"{base_url}/app_detail/{self.APP_NAME}")
        assert r.status_code == 200

    # -- proxy: verify the cloned app works --

    def test_proxy_works(self, admin_session, config):
        """Verify the app deployed from a Git URL is reachable through the proxy."""
        url = f"{_app_url(config, self.APP_NAME)}/health"
        deadline = time.time() + 30
        last_status = None
        last_err = None
        while time.time() < deadline:
            try:
                r = admin_session.get(url, timeout=2)
                last_status = r.status_code
                if r.status_code == 200:
                    assert r.json() == {"status": "ok"}
                    return
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
            time.sleep(1)
        pytest.fail(
            f"Git-deployed app did not respond within timeout (last_status={last_status}, last_err={last_err})"
        )

    # -- verify it was cloned (not copied) --

    def test_cloned_repo_is_git_repo(self, config):
        """The remote-repo deploy should git-clone, leaving a .git directory."""
        repo_dir = os.path.join(
            config.temporary_data_dir,
            "app_temp_data",
            self.APP_NAME,
            "repo",
        )
        git_dir = os.path.join(repo_dir, ".git")
        assert os.path.isdir(repo_dir), f"Cloned repo not found at {repo_dir}"
        assert os.path.isdir(git_dir), ".git directory not found — app may have been copied instead of cloned"

    # -- reload (exercises the git-pull path) --

    def test_reload_does_git_pull(self, admin_session, config):
        """Reload the Git-deployed app — should do 'git pull' instead of copytree."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, self.APP_NAME)
        r = admin_session.post(
            f"{base_url}/reload_app/{app_id}",
            timeout=120,
        )
        assert r.status_code == 200

        # Wait for the app to come back after reload
        url = f"{_app_url(config, self.APP_NAME)}/health"
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                r = admin_session.get(url, timeout=2)
                if r.status_code == 200:
                    return
            except (requests.ConnectionError, requests.Timeout):
                pass
            time.sleep(1)
        pytest.fail("Git-deployed app did not come back after reload")

    # -- remove + cleanup --

    def test_remove(self, admin_session, config):
        """Remove the Git-deployed app."""
        base_url = _zone_url(config)
        app_id = app_id_for(admin_session, base_url, self.APP_NAME)
        r = admin_session.post(
            f"{base_url}/remove_app/{app_id}",
        )
        assert r.status_code == 202
        wait_app_removed(admin_session, base_url, self.APP_NAME)

    def test_proxy_gone_after_remove(self, admin_session, config):
        """After removal, proxied requests should 404."""
        r = admin_session.get(
            f"{_app_url(config, self.APP_NAME)}/health",
            timeout=2,
        )
        assert r.status_code == 404

    def test_data_cleaned_up(self, config):
        """App data and temp directories should be deleted after removal."""
        app_data = os.path.join(
            config.persistent_data_dir,
            "app_data",
            self.APP_NAME,
        )
        app_temp = os.path.join(
            config.temporary_data_dir,
            "app_temp_data",
            self.APP_NAME,
        )
        assert not os.path.exists(app_data)
        assert not os.path.exists(app_temp)

    def test_container_cleaned_up(self):
        """Container should be removed after app removal."""
        r = subprocess.run(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                f"name=openhost-{self.APP_NAME}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert f"openhost-{self.APP_NAME}" not in r.stdout
