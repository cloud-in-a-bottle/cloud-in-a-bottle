"""
End-to-end integration tests for the Cloud in a Bottle router.

The full production flow is: build outer VM -> boot VM -> start router inside
VM -> deploy apps.  The VM step requires Linux with KVM and diskimage-builder,
so these tests run the router directly on the host and exercise the rootless
podman runtime natively.  This covers all the same code paths the router
would use inside the VM.
"""

import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import requests

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
from compute_space.tests.local_stack import create_bare_git_repo
from compute_space.tests.utils import app_id_for
from compute_space.tests.utils import managed_router
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

    def test_setup_get_redirects_to_login_if_already_set_up(self, admin_session, config):
        """GET /setup (e.g. the claim link) redirects to /login when owner already exists,
        so the claim link keeps working instead of dead-ending."""
        base_url = _zone_url(config)
        r = requests.get(f"{base_url}/setup?claim=whatever", allow_redirects=False)
        assert r.status_code == 302
        assert r.headers["Location"] == "/"

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
        create_bare_git_repo(os.path.join(_FIXTURES_DIR, "test_app"), bare_path)
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
class TestContainerE2E:
    """
    End-to-end test of the container deployment path: deploy, interact via
    the proxy, stop/reload, remove-with-keep-data + redeploy, container-
    engine-restart recovery, and a final full removal. One shared
    build+deploy of ``test-app`` on the module's shared router covers every
    scenario.

    DEFINITION ORDER IS LOAD-BEARING: each test builds on state left by the
    previous one. Do not reorder methods, and expect ``-k`` subsets / ``--ff``
    to break this.
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

    # -- remove with keep_data, then redeploy and verify data survived --

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

    # -- container-engine restart recovery, then final full removal --

    @pytest.fixture(scope="class")
    @classmethod
    def restarted_router(cls, admin_session, config, router_process):
        """Simulate a VM reboot: stop the shared router and container, then boot a
        replacement so check_app_status() rebuilds the dead container. Yields the
        replacement router; both tests below share this one restart.

        Wraps the whole risky window (stop, restart, and whatever the tests using
        this fixture do with it) so any failure force-removes the container instead
        of leaving it stopped/broken for whatever deploys next under the same name
        (e.g. tests/test_services_e2e.py's test_app fixture, run as a separate
        pytest invocation in the same CI job) -- mirroring the try/finally the
        original standalone restart test had around its own isolated container.
        """
        container_name = "openhost-test-app"
        try:
            _stop_router_process(router_process)

            result = subprocess.run(
                ["podman", "stop", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, f"podman stop failed: {result.stderr}"

            result = subprocess.run(
                ["podman", "inspect", "--format", "{{.State.Status}}", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.stdout.strip() == "exited", "Container should be exited after podman stop"

            # check_app_status() does a synchronous image rebuild during startup,
            # so /health won't respond until the rebuild finishes -- allow extra
            # time.  Same config/db as the original router, so admin_session's
            # DB-backed cookies keep working against this replacement with no
            # re-login needed.
            with managed_router(config, startup_timeout=180) as new_router:
                yield new_router
        except Exception:
            container_cleanup(container_name, "test-app")
            raise

    def test_container_engine_restart_recovers(self, admin_session, config, restarted_router):
        """check_app_status() detects the dead container on boot and rebuilds it."""
        base_url = _zone_url(config)
        db_path = config.db_path
        container_name = "openhost-test-app"

        deadline = time.time() + 15
        container_running = False
        while time.time() < deadline:
            result = subprocess.run(
                ["podman", "inspect", "--format", "{{.State.Status}}", container_name],
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
                db = sqlite3.connect(db_path)
                try:
                    db.row_factory = sqlite3.Row
                    row = db.execute(
                        "SELECT local_port FROM apps WHERE name = ?",
                        ("test-app",),
                    ).fetchone()
                finally:
                    db.close()
                if row:
                    r = requests.get(f"http://127.0.0.1:{row['local_port']}/health", timeout=2)
                    if r.status_code == 200:
                        container_healthy = True
                        break
            except Exception as exc:
                last_poll_error = exc
            time.sleep(1)
        assert container_healthy, (
            f"Container should be healthy and serving traffic (last poll error: {last_poll_error})"
        )

        app_id = app_id_for(admin_session, base_url, "test-app")
        assert app_id is not None, "App row should exist after rebuild"
        r = admin_session.get(f"{base_url}/app_detail/test-app")
        assert r.status_code == 200

        # Poll the DB for status='running' (background thread may still be
        # finishing the _wait_for_ready() health check).
        deadline = time.time() + 30
        db_status = None
        while time.time() < deadline:
            try:
                poll_db = sqlite3.connect(db_path)
                try:
                    poll_db.row_factory = sqlite3.Row
                    poll_row = poll_db.execute(
                        "SELECT status FROM apps WHERE name = ?",
                        ("test-app",),
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

    def test_final_removal_after_recovery(self, admin_session, config, restarted_router):
        """Full removal after recovery deletes the container, data, and temp dirs."""
        base_url = _zone_url(config)
        container_name = "openhost-test-app"

        app_id = app_id_for(admin_session, base_url, "test-app")
        assert app_id is not None
        r = admin_session.post(f"{base_url}/remove_app/{app_id}")
        assert r.status_code == 202
        wait_app_removed(admin_session, base_url, "test-app")

        r = admin_session.get(f"{_app_url(config, 'test-app')}/health", timeout=2)
        assert r.status_code == 404, "Proxied requests should 404 after removal"

        app_data = os.path.join(config.persistent_data_dir, "app_data", "test-app")
        app_temp = os.path.join(config.temporary_data_dir, "app_temp_data", "test-app")
        assert not os.path.exists(app_data), "Full remove should delete persistent data"
        assert not os.path.exists(app_temp), "Full remove should delete temp data"

        result = subprocess.run(
            ["podman", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert container_name not in result.stdout, "Container should be removed after app removal"
