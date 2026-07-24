"""Cloud E2E tests for OpenHost self-host deployment.

Requires ``OPENHOST_DOMAIN`` env var pointing at a running instance.
See ``tests/gcp/`` or ``tests/ec2/`` for infrastructure setup scripts.
"""

import asyncio
import os
import secrets
import shlex
import socket
import ssl
import string
import subprocess
import time

import pytest
import requests
import websockets

from compute_space.tests.utils import app_id_for
from compute_space.tests.utils import wait_app_removed
from compute_space.tests.utils import wait_app_running
from tests.helpers import poll_endpoint

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOMAIN = os.environ.get("OPENHOST_DOMAIN", "")
APP_DEPLOY_TIMEOUT_S = 300
# Apps live in the synced repo on the host (deployed via ansible).
TEST_APP_PATH = "/home/host/openhost/apps/test_app"
# Generate a random password per test run since instances are publicly routable.
OWNER_PASSWORD = "E2e!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
# Claim token written by ansible at deploy time; required to POST /setup.
CLAIM_TOKEN = os.environ.get("OPENHOST_CLAIM_TOKEN", "")


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def domain():
    if not DOMAIN:
        pytest.skip("OPENHOST_DOMAIN not set")
    return DOMAIN


@pytest.fixture(scope="module")
def router_url(domain):
    return f"https://{domain}"


@pytest.fixture(scope="module")
def session(router_url):
    """Starts unauthenticated; test_02 adds auth cookies via /setup."""
    return requests.Session()


@pytest.fixture(scope="module")
def healthy_router(router_url):
    """Block until the router responds to /health over HTTPS."""
    poll_endpoint(
        requests.Session(),
        f"{router_url}/health",
        timeout=420,
        interval=10,
        fail_msg=f"Router at {router_url} did not become healthy",
    )
    return True


# ---------------------------------------------------------------------------
# Tests -- executed in order (test_01, test_02, ...)
# ---------------------------------------------------------------------------


class TestSelfHost:
    """E2E tests for self-host deployment.

    Tests are numbered and must run in order since later tests depend on
    state created by earlier ones (owner account, deployed app, etc.).
    """

    # -- 1. Health check ---------------------------------------------------

    def test_01_health(self, router_url, healthy_router):
        """Router responds to /health with valid JSON."""
        r = requests.get(f"{router_url}/health", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "security" not in data

    # -- 2. Setup (create owner) -------------------------------------------

    def test_02_setup(self, session, router_url, healthy_router):
        """First visitor to /setup becomes the owner (after passing the claim token)."""
        assert CLAIM_TOKEN, "OPENHOST_CLAIM_TOKEN must be set by e2e-setup.sh"
        claim_params = {"claim": CLAIM_TOKEN}
        r = session.get(f"{router_url}/setup", params=claim_params, timeout=10)
        assert r.status_code == 200, f"/setup returned {r.status_code}: {r.text[:500]}"

        r = session.post(
            f"{router_url}/setup",
            params=claim_params,
            data={
                "password": OWNER_PASSWORD,
                "confirm_password": OWNER_PASSWORD,
                "claim": CLAIM_TOKEN,
            },
            timeout=30,
            allow_redirects=False,
        )
        # setup_app returns 200 + meta-refresh + Set-Cookie, then triggers a
        # restart (it can't 302 synchronously without racing the listener
        # shutdown). Treat the "Restarting…" page as the success signal and
        # wait for the full app to come back up before hitting /dashboard.
        assert r.status_code == 200, f"Setup POST returned {r.status_code}: {r.text[:500]}"
        assert "Restarting" in r.text, f"Expected restart page, got: {r.text[:500]}"

        set_cookies = r.headers.get("Set-Cookie", "")
        assert "session_token" in set_cookies, f"Setup must set session_token cookie. Set-Cookie: {set_cookies[:300]}"

        session.cookies.update(r.cookies)
        poll_endpoint(
            requests.Session(),
            f"{router_url}/health",
            timeout=120,
            interval=2,
            fail_msg=f"Router at {router_url} did not come back after setup restart",
        )
        r = session.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    # -- 3. Dashboard access -----------------------------------------------

    def test_03_dashboard_requires_auth(self, router_url, healthy_router):
        """Unauthenticated requests to /dashboard are rejected."""
        r = requests.get(
            f"{router_url}/dashboard",
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")

    def test_03b_dashboard_with_auth(self, session, router_url):
        """Authenticated session can access /dashboard."""
        r = session.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    # -- 4. Deploy test-app ------------------------------------------------

    def test_04_deploy_test_app(self, session, router_url):
        """Deploy the test-app from the apps directory on the host."""
        repo_url = f"file://{TEST_APP_PATH}"
        r = session.post(
            f"{router_url}/api/add_app",
            json={"repo_url": repo_url},
            timeout=120,
        )
        assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert data.get("app_name") == "test-app"

    # -- 5. Wait for app running -------------------------------------------

    def test_05_wait_app_running(self, session, router_url):
        """Poll app status until test-app reports 'running'."""
        wait_app_running(session, router_url, "test-app", timeout=APP_DEPLOY_TIMEOUT_S)

    # -- 6. Unknown-path 404 ----------------------------------------------

    def test_06_unknown_app_404(self, router_url, session):
        """Requests to unknown zone paths return 404."""
        r = session.get(f"{router_url}/no-such-app/anything", timeout=5)
        assert r.status_code == 404

    # -- 7. Subdomain routing ----------------------------------------------

    def test_07_subdomain_routing(self, session, domain):
        """test-app responds via subdomain routing (test-app.<domain>)."""
        app_url = f"https://test-app.{domain}"
        r = poll_endpoint(session, f"{app_url}/health", fail_msg=f"test-app not responding via subdomain at {app_url}")
        assert r.json() == {"status": "ok"}

    def test_07b_subdomain_root(self, session, domain):
        """Subdomain routing returns app metadata."""
        app_url = f"https://test-app.{domain}"
        r = session.get(f"{app_url}/", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "test-app"

    def test_07c_subdomain_post(self, session, domain):
        """POST request is proxied to the app via subdomain."""
        app_url = f"https://test-app.{domain}"
        r = session.post(f"{app_url}/submit", data="hello world", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["method"] == "POST"
        assert data["body"] == "hello world"
        assert data["path"] == "/submit"

    def test_07d_subdomain_forwarded_headers(self, session, domain):
        """Proxy sets X-Forwarded-* headers and strips spoofed ones."""
        app_url = f"https://test-app.{domain}"
        r = session.get(
            f"{app_url}/echo-headers",
            headers={
                "X-Custom-Test": "test-value",
                "X-Forwarded-For": "attacker-ip",
                "X-Forwarded-Proto": "evil",
                "X-Forwarded-Host": "evil.example.com",
            },
            timeout=5,
        )
        assert r.status_code == 200
        # ASGI passes header names lowercase, so the backend sees lowercase keys.
        # HTTP headers are case-insensitive per spec — normalize for lookup.
        headers = {k.lower(): v for k, v in r.json()["headers"].items()}
        # Custom headers are forwarded
        assert headers.get("x-custom-test") == "test-value"
        # X-Forwarded-* are set by the proxy
        assert "x-forwarded-for" in headers
        assert "x-forwarded-host" in headers
        # The client connected over HTTPS (Caddy terminates TLS); the app must
        # see that, even though the router talks plain HTTP to Caddy internally.
        assert headers.get("x-forwarded-proto") == "https"
        # Spoofed values are overwritten (Caddy drops them before the router).
        assert "attacker-ip" not in headers.get("x-forwarded-for", "")
        assert headers.get("x-forwarded-host") != "evil.example.com"

    def test_07e_subdomain_unauth_rejected(self, domain):
        """Unauthenticated subdomain request to a non-public path is rejected."""
        app_url = f"https://test-app.{domain}"
        r = requests.get(
            f"{app_url}/echo-headers",
            allow_redirects=False,
            timeout=10,
        )
        # Non-public path without auth should be rejected
        assert r.status_code in (401, 302)

    # -- 8. App lifecycle: stop and reload ---------------------------------

    def test_08_stop_app(self, session, router_url, domain):
        """Stop the app -- proxied requests should fail afterward."""
        app_id = app_id_for(session, router_url, "test-app")
        r = session.post(f"{router_url}/stop_app/{app_id}", timeout=30)
        assert r.status_code == 200
        time.sleep(2)
        r = session.get(f"https://test-app.{domain}/health", timeout=5)
        assert r.status_code in (404, 502, 503)

    def test_08b_reload_app(self, session, router_url, domain):
        """Reload the app -- rebuilds and restarts the container."""
        app_id = app_id_for(session, router_url, "test-app")
        r = session.post(f"{router_url}/reload_app/{app_id}", timeout=120)
        assert r.status_code == 200
        r = poll_endpoint(
            session,
            f"https://test-app.{domain}/health",
            timeout=APP_DEPLOY_TIMEOUT_S,
            interval=5,
            fail_msg="test-app did not come back after reload",
        )
        assert r.json() == {"status": "ok"}

    # -- 9. Multiple concurrent apps ---------------------------------------

    def test_09_deploy_second_app(self, session, router_url):
        """Deploy a second instance of the test app with a different name."""
        r = session.post(
            f"{router_url}/api/add_app",
            json={"repo_url": f"file://{TEST_APP_PATH}", "app_name": "test-app-2"},
            timeout=120,
        )
        assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:500]}"
        assert r.json().get("app_name") == "test-app-2"
        wait_app_running(session, router_url, "test-app-2", timeout=APP_DEPLOY_TIMEOUT_S)

    def test_09b_subdomain_isolation(self, session, domain):
        """Both apps respond via their own subdomains."""
        r1 = session.get(f"https://test-app.{domain}/health", timeout=5)
        assert r1.status_code == 200
        assert r1.json() == {"status": "ok"}

        r2 = session.get(f"https://test-app-2.{domain}/health", timeout=5)
        assert r2.status_code == 200
        assert r2.json() == {"status": "ok"}

    def test_09d_apps_list(self, session, router_url):
        """GET /api/apps returns both deployed apps."""
        r = session.get(f"{router_url}/api/apps", timeout=10)
        assert r.status_code == 200
        data = r.json()
        # /api/apps returns a list of {app_id, name, status, error_message}
        names = {entry.get("name") for entry in data}
        assert "test-app" in names, f"test-app not in /api/apps response: {names}"
        assert "test-app-2" in names, f"test-app-2 not in /api/apps response: {names}"

    def test_09e_remove_second_app(self, session, router_url, domain):
        """Remove the second app; first app still works."""
        app_id = app_id_for(session, router_url, "test-app-2")
        r = session.post(f"{router_url}/remove_app/{app_id}", timeout=30)
        assert r.status_code == 202
        wait_app_removed(session, router_url, "test-app-2")

        # Second app is gone
        r = session.get(f"https://test-app-2.{domain}/health", timeout=5)
        assert r.status_code == 404

        # First app still works
        r = session.get(f"https://test-app.{domain}/health", timeout=5)
        assert r.status_code == 200

    # -- 10. API tokens ----------------------------------------------------

    def test_10_create_api_token(self, session, router_url):
        """Create an API token and use it to access a protected endpoint."""
        r = session.post(
            f"{router_url}/api/tokens",
            json={"name": "e2e-test-token", "expiry_hours": "1"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert data["name"] == "e2e-test-token"

        # Store token on the class for later tests
        TestSelfHost._api_token = data["token"]

    def test_10b_use_api_token(self, router_url):
        """API token (Bearer header, no cookies) can access /api/apps."""
        token = getattr(TestSelfHost, "_api_token", None)
        assert token, "API token not set by test_10"

        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_10c_invalid_token_rejected(self, router_url):
        """A bogus Bearer token is rejected."""
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": "Bearer bogus-token-value"},
            allow_redirects=False,
            timeout=10,
        )
        # Rejected -- redirects to login or returns 401
        assert r.status_code in (302, 401)

    def test_10d_list_and_delete_token(self, session, router_url):
        """List tokens, find ours, delete it, verify it stops working."""
        r = session.get(f"{router_url}/api/tokens", timeout=10)
        assert r.status_code == 200
        tokens = r.json()
        matching = [t for t in tokens if t["name"] == "e2e-test-token"]
        assert matching, f"Token not found in list: {tokens}"
        token_id = matching[0]["id"]

        # Delete it
        r = session.delete(f"{router_url}/api/tokens/{token_id}", timeout=10)
        assert r.status_code == 204

        # Token should no longer work
        token = getattr(TestSelfHost, "_api_token", None)
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code in (302, 401)

    # -- 11. Login / logout ------------------------------------------------

    def test_11_login_bad_password(self, router_url):
        """Login with wrong credentials is rejected."""
        r = requests.post(
            f"{router_url}/login",
            data={"username": "admin", "password": "wrong-password"},
            timeout=10,
        )
        assert r.status_code == 200
        assert "Invalid password" in r.text or "invalid" in r.text.lower()

    def test_11b_login_good_password(self, router_url):
        """Login with correct credentials sets auth cookies."""
        login_session = requests.Session()
        login_session.verify = True
        r = login_session.post(
            f"{router_url}/login",
            data={"username": "admin", "password": OWNER_PASSWORD, "next": "/dashboard"},
            allow_redirects=False,
            timeout=10,
        )
        # Should redirect to the requested next URL (/dashboard)
        assert r.status_code == 302
        assert "/dashboard" in r.headers.get("Location", "")
        # Session cookie should be set
        set_cookies = r.headers.get("Set-Cookie", "")
        assert "session_token" in set_cookies

        # Follow redirect, verify dashboard works
        login_session.cookies.update(r.cookies)
        r = login_session.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    def test_11c_logout(self, router_url):
        """Logout clears the session."""
        # Create a fresh session, log in, then log out
        s = requests.Session()
        s.verify = True
        r = s.post(
            f"{router_url}/login",
            data={"username": "admin", "password": OWNER_PASSWORD},
            timeout=10,
        )
        # Verify we're logged in
        r = s.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200

        # Logout
        r = s.post(f"{router_url}/logout", allow_redirects=False, timeout=10)
        assert r.status_code == 302

        # Dashboard should no longer be accessible
        r = s.get(f"{router_url}/dashboard", allow_redirects=False, timeout=10)
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")

    # -- 12. Storage and system endpoints ----------------------------------

    def test_12_storage_status(self, session, router_url):
        """GET /api/storage-status returns valid storage info."""
        r = session.get(f"{router_url}/api/storage-status", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        # Unified disk metric (no separate persistent/temporary keys)
        assert "disk" in data
        assert "total_bytes" in data["disk"]
        assert "used_bytes" in data["disk"]
        assert "free_bytes" in data["disk"]
        assert "persistent" not in data
        assert "temporary" not in data

    def test_12b_app_logs(self, session, router_url):
        """GET /app_logs/<app_id> returns log content."""
        app_id = app_id_for(session, router_url, "test-app")
        r = session.get(f"{router_url}/app_logs/{app_id}", timeout=10)
        assert r.status_code == 200

    def test_12c_compute_space_logs(self, session, router_url):
        """GET /api/compute_space_logs returns router logs."""
        r = session.get(f"{router_url}/api/compute_space_logs", timeout=10)
        assert r.status_code == 200

    def test_12d_websocket_echo(self, session, domain):
        """WebSocket proxy: connect to test-app's /ws endpoint and echo a message."""
        # Extract auth cookies from the session for the WS handshake
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in session.cookies)

        async def _ws_echo():
            uri = f"wss://test-app.{domain}/ws"
            extra_headers = {"Cookie": cookie_header}
            async with websockets.connect(
                uri,
                additional_headers=extra_headers,
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                await ws.send("hello from e2e test")
                response = await asyncio.wait_for(ws.recv(), timeout=5)
                assert response == "hello from e2e test"

        asyncio.run(_ws_echo())

    # -- 12e-12t. Diagnostics ----------------------------------------------
    # Exercised while ``test-app`` is deployed (test_04) and before it is
    # removed (test_14), so per-app diagnostics has a real, running app.

    def test_12e_platform_diagnostics_top_level(self, session, router_url):
        """GET /api/diagnostics returns a full bundle with all top-level keys."""
        r = session.get(f"{router_url}/api/diagnostics", timeout=60)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
        assert r.headers.get("content-type", "").startswith("application/json")
        d = r.json()
        expected = {
            "schema_version",
            "generated_at",
            "zone_domain",
            "openhost",
            "system",
            "container_runtime",
            "dependencies",
            "storage",
            "resource_pressure",
            "reachability",
            "apps",
        }
        assert expected <= set(d), f"missing keys: {expected - set(d)}"
        assert d["schema_version"] == 2
        assert d["zone_domain"] == DOMAIN

    def test_12f_platform_openhost_git(self, session, router_url):
        """The openhost section reports a real git checkout (sha + remote)."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        oh = d["openhost"]
        for key in ("branch", "sha", "short_sha", "dirty", "remote_url"):
            assert key in oh, f"openhost missing {key}"
        assert oh["sha"], "openhost sha should be populated on a git deploy"
        assert isinstance(oh["dirty"], bool)

    def test_12g_platform_system_and_runtime(self, session, router_url):
        """System info + container runtime are populated with real values."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        sysinfo = d["system"]
        assert sysinfo["system"] == "Linux"
        assert sysinfo["python_version"]
        assert isinstance(sysinfo["cpu_count"], int) and sysinfo["cpu_count"] >= 1
        rt = d["container_runtime"]
        assert rt["available"] is True, f"podman should be available: {rt}"
        assert rt["version"], "podman version should be populated"
        # OpenHost runs rootless podman.
        assert rt["rootless"] is True

    def test_12h_platform_storage_and_deps(self, session, router_url):
        """Storage disk metrics and key dependency versions are present."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        disk = d["storage"]["disk"]
        assert disk["total_bytes"] > 0
        assert disk["free_bytes"] >= 0
        deps = d["dependencies"]
        assert isinstance(deps, dict) and deps, "dependencies should be a non-empty map"
        assert "litestar" in deps

    def test_12i_platform_resource_pressure(self, session, router_url):
        """Resource-pressure memory metrics are present and self-consistent."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        rp = d["resource_pressure"]
        assert rp["memory_total_bytes"] and rp["memory_total_bytes"] > 0
        assert rp["memory_available_bytes"] is not None
        # used% is a percentage in [0, 100] when both memory numbers are present.
        if rp["memory_used_percent"] is not None:
            assert 0.0 <= rp["memory_used_percent"] <= 100.0
        assert isinstance(rp["cpu_count"], int) and rp["cpu_count"] >= 1

    def test_12j_platform_reachability_shape(self, session, router_url):
        """Reachability is a list of probes with the expected fields, and the
        static github probe is present and reachable from the instance."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        reach = d["reachability"]
        assert isinstance(reach, list) and reach
        labels = {p["label"] for p in reach}
        assert "github" in labels
        for p in reach:
            assert {"label", "url", "reachable", "status_code", "latency_ms", "error"} <= set(p)
        gh = next(p for p in reach if p["label"] == "github")
        assert gh["reachable"] is True, f"github should be reachable: {gh}"

    def test_12k_reachability_excludes_cert_api(self, session, router_url):
        """The cert-api base URL reachability probe was removed."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        labels = {p["label"] for p in d["reachability"]}
        assert "cert_api" not in labels

    def test_12l_platform_apps_summary(self, session, router_url):
        """Every installed app appears in the summary with a stable shape;
        the running test-app reports healthy with live resource usage."""
        d = session.get(f"{router_url}/api/diagnostics", timeout=60).json()
        apps = d["apps"]
        assert isinstance(apps, list) and apps
        names = {a["name"] for a in apps}
        assert "test-app" in names
        for a in apps:
            assert {"app_id", "name", "status", "version", "health", "resources", "git"} <= set(a)
            assert {"running", "cpu_cores_limit", "memory_mb_limit"} <= set(a["resources"])
            assert {"checked", "healthy", "status_code", "checked_path"} <= set(a["health"])
        test_app = next(a for a in apps if a["name"] == "test-app")
        assert test_app["status"] == "running"
        assert test_app["resources"]["running"] is True
        assert test_app["health"]["healthy"] is True

    def test_12m_platform_download_header(self, session, router_url):
        """?download=1 sets a timestamped attachment filename."""
        r = session.get(f"{router_url}/api/diagnostics?download=1", timeout=60)
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd and DOMAIN in cd and cd.endswith('.json"')
        # Still valid JSON.
        assert r.json()["schema_version"] == 2

    def test_12n_per_app_diagnostics(self, session, router_url):
        """GET /api/app_diagnostics/<id> returns a self-contained per-app bundle."""
        app_id = app_id_for(session, router_url, "test-app")
        r = session.get(f"{router_url}/api/app_diagnostics/{app_id}", timeout=60)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
        d = r.json()
        expected = {
            "schema_version",
            "app_id",
            "name",
            "status",
            "version",
            "git",
            "health",
            "resources",
            "container_runtime",
            "system",
            "resource_pressure",
            "zone_domain",
        }
        assert expected <= set(d), f"missing keys: {expected - set(d)}"
        assert d["schema_version"] == 2
        assert d["name"] == "test-app"
        assert d["app_id"] == app_id

    def test_12o_per_app_download_header(self, session, router_url):
        """Per-app ?download=1 sets a timestamped attachment filename."""
        app_id = app_id_for(session, router_url, "test-app")
        r = session.get(f"{router_url}/api/app_diagnostics/{app_id}?download=1", timeout=60)
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd and "test-app" in cd

    def test_12p_diagnostics_auth_required(self, router_url):
        """Both diagnostics endpoints require owner auth (no cookies/token)."""
        for path in ("/api/diagnostics", "/api/app_diagnostics/AAAAAAAAAAAA"):
            r = requests.get(f"{router_url}{path}", allow_redirects=False, timeout=10)
            assert r.status_code in (302, 401), f"{path} not gated: {r.status_code}"

    def test_12q_diagnostics_bad_token_rejected(self, router_url):
        """A bogus Bearer token cannot read diagnostics."""
        r = requests.get(
            f"{router_url}/api/diagnostics",
            headers={"Authorization": "Bearer bogus-token-value"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code in (302, 401)

    def test_12r_diagnostics_method_not_allowed(self, session, router_url):
        """The GET-only diagnostics endpoint rejects other methods."""
        for method in ("POST", "PUT", "DELETE"):
            r = session.request(method, f"{router_url}/api/diagnostics", timeout=10)
            assert r.status_code == 405, f"{method} -> {r.status_code}"

    def test_12s_per_app_diagnostics_errors(self, session, router_url):
        """Invalid-format app_id -> 400; valid-format-but-missing -> 404."""
        r = session.get(f"{router_url}/api/app_diagnostics/not-a-valid-id!!", timeout=10)
        assert r.status_code == 400
        # 12 base58 chars: valid format, no such app.
        r = session.get(f"{router_url}/api/app_diagnostics/ABCDEFGH1234", timeout=10)
        assert r.status_code == 404

    def test_12t_diagnostics_page_renders(self, session, router_url):
        """The Diagnostics dashboard page loads and links the JSON endpoint."""
        r = session.get(f"{router_url}/diagnostics/", timeout=30)
        assert r.status_code == 200
        assert "Diagnostics" in r.text
        assert "/api/diagnostics" in r.text

    # -- 13. TLS certificate -----------------------------------------------

    def test_13_tls_cert(self, domain):
        """TLS cert is valid and covers the wildcard domain."""
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(
            socket.create_connection((domain, 443), timeout=10),
            server_hostname=domain,
        ) as sock:
            cert = sock.getpeercert()

        cert_names = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
        assert any(domain in name or name == f"*.{domain}" for name in cert_names), (
            f"Cert SANs {cert_names} do not cover {domain}"
        )

        issuer = dict(x[0] for x in cert.get("issuer", []))
        org = issuer.get("organizationName", "")
        # Google Trust Services is the primary CA (via ACME DNS-01 challenge).
        # Let's Encrypt / ISRG accepted as fallback.
        valid_issuers = ("Google Trust Services", "Let's Encrypt", "ISRG")
        assert any(name in org for name in valid_issuers), (
            f"Cert issuer '{org}' is not from a recognized CA (expected one of {valid_issuers})"
        )

    def test_13b_wildcard_tls(self, domain):
        """TLS cert covers app subdomains via wildcard."""
        ctx = ssl.create_default_context()
        app_hostname = f"test-app.{domain}"
        with ctx.wrap_socket(
            socket.create_connection((domain, 443), timeout=10),
            server_hostname=app_hostname,
        ) as sock:
            cert = sock.getpeercert()

        cert_names = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
        assert any(name == f"*.{domain}" for name in cert_names), (
            f"Cert SANs {cert_names} do not include wildcard *.{domain}"
        )

    # -- 13c. S3 archive backend (MinIO) -----------------------------------

    def test_13c_deploy_minio(self, session, router_url):
        """Deploy MinIO from its public GitHub repo."""
        r = session.post(
            f"{router_url}/api/add_app",
            json={"repo_url": "https://github.com/imbue-openhost/openhost-minio"},
            timeout=120,
        )
        assert r.status_code == 200, f"add_app minio failed: {r.status_code}: {r.text[:500]}"
        assert r.json().get("app_name") == "minio"
        wait_app_running(session, router_url, "minio", timeout=APP_DEPLOY_TIMEOUT_S)

    def test_13d_deploy_file_browser(self, session, router_url, domain):
        """Deploy file-browser (built-in) to read MinIO credentials and archive files."""
        # file-browser may already be deployed as a default app; skip if so
        existing = app_id_for(session, router_url, "file-browser")
        if existing:
            TestSelfHost._fb_was_preexisting = True
            # Verify it's running
            r = session.get(f"{router_url}/api/app_status/{existing}", timeout=10)
            assert r.json()["status"] == "running"
            return
        TestSelfHost._fb_was_preexisting = False
        r = session.post(
            f"{router_url}/api/add_app",
            json={"repo_url": "file:///home/host/openhost/apps/file_browser"},
            timeout=120,
        )
        assert r.status_code == 200, f"add_app file-browser failed: {r.status_code}: {r.text[:500]}"
        wait_app_running(session, router_url, "file-browser", timeout=APP_DEPLOY_TIMEOUT_S)

    def test_13e_read_minio_credentials(self, session, domain):
        """Read MinIO root credentials via file-browser."""
        fb_url = f"https://file-browser.{domain}"
        # file-browser (dufs) serves files directly; credentials are at
        # /app_data/minio/config/root-credentials.txt
        r = poll_endpoint(
            session,
            f"{fb_url}/app_data/minio/config/root-credentials.txt",
            timeout=60,
            interval=5,
            fail_msg="Could not read MinIO credentials via file-browser",
        )
        cred_text = r.text
        # Parse: lines like "export MINIO_ROOT_USER='...'"
        creds = {}
        for line in cred_text.splitlines():
            line = line.strip()
            if line.startswith("export MINIO_ROOT_USER="):
                creds["user"] = line.split("=", 1)[1].strip("'\"")
            elif line.startswith("export MINIO_ROOT_PASSWORD="):
                creds["password"] = line.split("=", 1)[1].strip("'\"")
        assert "user" in creds, f"Could not parse MINIO_ROOT_USER from: {cred_text[:200]}"
        assert "password" in creds, f"Could not parse MINIO_ROOT_PASSWORD from: {cred_text[:200]}"
        TestSelfHost._minio_user = creds["user"]
        TestSelfHost._minio_password = creds["password"]

    def test_13f_create_minio_bucket(self, domain):
        """Create a test bucket in MinIO using the mc CLI on the host via SSH."""
        minio_user = getattr(TestSelfHost, "_minio_user", None)
        minio_password = getattr(TestSelfHost, "_minio_password", None)
        assert minio_user and minio_password, "MinIO credentials not available"

        bucket = "openhost-e2e-archive"
        # MinIO S3 API is on port 9106 (host-mapped), accessible as localhost on the host
        endpoint = "http://localhost:9106"
        ssh_key = os.environ.get("OPENHOST_SSH_KEY", "")
        public_ip = os.environ.get("OPENHOST_PUBLIC_IP", "")
        assert ssh_key and public_ip, "SSH credentials not available for bucket creation"

        ssh_opts = f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -i {ssh_key}"
        # Install mc (MinIO client), configure alias, create bucket.
        # Detect the VM's arch so this works on both amd64 and arm64 hosts.
        commands = (
            'mcarch=$(case "$(uname -m)" in (aarch64|arm64) echo linux-arm64;; *) echo linux-amd64;; esac) && '
            "curl -sL https://dl.min.io/client/mc/release/$mcarch/mc -o /tmp/mc && chmod +x /tmp/mc && "
            f"/tmp/mc alias set e2e {endpoint} '{minio_user}' '{minio_password}' && "
            f"/tmp/mc mb --ignore-existing e2e/{bucket} && "
            # Second bucket for the later s3->s3 migration test (13k).
            f"/tmp/mc mb --ignore-existing e2e/{bucket}-2"
        )
        result = subprocess.run(
            f"ssh {ssh_opts} host@{public_ip} {shlex.quote(commands)}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Bucket creation failed: {result.stderr}"
        TestSelfHost._minio_bucket = bucket
        TestSelfHost._minio_bucket_2 = f"{bucket}-2"
        TestSelfHost._minio_endpoint = endpoint

    # Content written to the LOCAL archive before S3 is configured; it must
    # survive the local->S3 migration (juicefs sync + config) byte-for-byte.
    _PRE_MIGRATION_PATH = "app_archive/file-browser/pre-migration.txt"
    _PRE_MIGRATION_CONTENT = "written on the LOCAL backend before S3 migration\n" * 64

    def test_13f2_seed_local_archive_before_migration(self, session, domain):
        """Write a file into the (local file-backed) archive BEFORE configuring
        S3, so test_13h2 can prove the migration preserved existing data."""
        fb_url = f"https://file-browser.{domain}"
        r = session.put(
            f"{fb_url}/{self._PRE_MIGRATION_PATH}",
            data=self._PRE_MIGRATION_CONTENT,
            headers={"Content-Type": "text/plain"},
            timeout=30,
        )
        assert r.status_code in (200, 201, 204), f"seed PUT failed: {r.status_code}: {r.text[:200]}"
        # Read back on the local backend to be sure it landed.
        r = session.get(f"{fb_url}/{self._PRE_MIGRATION_PATH}", timeout=10)
        assert r.status_code == 200
        assert r.text == self._PRE_MIGRATION_CONTENT

    def test_13f3_state_lists_local_archive_app(self, session, router_url):
        """Before migrating, the backend must report backend=local and list
        file-browser among the apps whose data an S3 upgrade will migrate."""
        r = session.get(f"{router_url}/api/storage/archive_backend", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] == "local", data
        assert "file-browser" in (data.get("local_archive_apps") or []), data

    def test_13g_configure_archive_backend(self, session, router_url):
        """Configure the archive backend to use the local MinIO instance."""
        minio_user = getattr(TestSelfHost, "_minio_user", None)
        minio_password = getattr(TestSelfHost, "_minio_password", None)
        bucket = getattr(TestSelfHost, "_minio_bucket", None)
        endpoint = getattr(TestSelfHost, "_minio_endpoint", None)
        assert all([minio_user, minio_password, bucket, endpoint])

        # Test connection first
        r = session.post(
            f"{router_url}/api/storage/archive_backend/test_connection",
            json={
                "s3_bucket": bucket,
                "s3_access_key_id": minio_user,
                "s3_secret_access_key": minio_password,
                "s3_endpoint": endpoint,
                "s3_region": "us-east-1",
                "s3_prefix": "",
            },
            timeout=30,
        )
        assert r.status_code == 200, f"test_connection failed: {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert data.get("ok"), f"test_connection not ok: {data}"

        # Configure
        r = session.post(
            f"{router_url}/api/storage/archive_backend/configure",
            json={
                "s3_bucket": bucket,
                "s3_access_key_id": minio_user,
                "s3_secret_access_key": minio_password,
                "s3_endpoint": endpoint,
                "s3_region": "us-east-1",
                "s3_prefix": "e2e-test",
                # NOTE: no juicefs_volume_name override — the local zone already
                # has a formatted volume ('openhost'); its objects live under
                # that prefix and the migration must keep using it.  Passing a
                # different volume name here is intentionally ignored by the
                # backend for a local->s3 upgrade.
                # We seeded local archive data in 13f2, so we must acknowledge
                # the migration or the API returns 409.
                "confirm_migrate_local": True,
            },
            timeout=600,
        )
        assert r.status_code == 200, f"configure failed: {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert data.get("backend") == "s3", f"configure didn't set backend to s3: {data}"

    def test_13h_verify_archive_backend_state(self, session, router_url):
        """Verify the archive backend reports as configured."""
        r = session.get(f"{router_url}/api/storage/archive_backend", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] == "s3"
        assert data["s3_bucket"] == "openhost-e2e-archive"

    def test_13h2_pre_migration_data_survived(self, session, domain):
        """The file written to the LOCAL archive in 13f2 must still be readable
        (byte-identical) now that the volume is S3-backed — proving the
        juicefs sync + config migration preserved existing data."""
        fb_url = f"https://file-browser.{domain}"
        # The app was recycled after migration (quiesced before the remount,
        # restarted after); give it a moment to come back and re-open the
        # now-S3-backed archive.
        r = poll_endpoint(
            session,
            f"{fb_url}/{self._PRE_MIGRATION_PATH}",
            timeout=90,
            interval=5,
            fail_msg="pre-migration archive file not readable after local->S3 migration",
        )
        assert r.text == self._PRE_MIGRATION_CONTENT, "pre-migration archive content changed across migration"

    def test_13i_archive_file_roundtrip(self, session, domain):
        """Write, read, and delete a file in the archive via file-browser."""
        fb_url = f"https://file-browser.{domain}"
        test_content = "e2e archive backend test file"

        # file-browser (dufs) supports PUT for uploads
        r = session.put(
            f"{fb_url}/app_archive/file-browser/e2e-test-file.txt",
            data=test_content,
            headers={"Content-Type": "text/plain"},
            timeout=30,
        )
        # dufs returns 201 Created or 204 No Content on PUT
        assert r.status_code in (200, 201, 204), f"PUT failed: {r.status_code}: {r.text[:200]}"

        # Read back
        r = session.get(f"{fb_url}/app_archive/file-browser/e2e-test-file.txt", timeout=10)
        assert r.status_code == 200
        assert r.text == test_content

        # Delete
        r = session.request(
            "DELETE",
            f"{fb_url}/app_archive/file-browser/e2e-test-file.txt",
            timeout=10,
        )
        assert r.status_code in (200, 204), f"DELETE failed: {r.status_code}: {r.text[:200]}"

        # Verify gone
        r = session.get(f"{fb_url}/app_archive/file-browser/e2e-test-file.txt", timeout=10)
        assert r.status_code == 404

    def test_13k_migrate_s3_to_s3(self, session, router_url, domain):
        """Migrate the archive from the first MinIO bucket to a SECOND bucket
        (s3->s3), and prove pre-migration data survives byte-identical and the
        roundtrip file is gone (it was deleted in 13i) — exercising the
        juicefs sync + config s3->s3 path on a live instance."""
        minio_user = getattr(TestSelfHost, "_minio_user", None)
        minio_password = getattr(TestSelfHost, "_minio_password", None)
        bucket2 = getattr(TestSelfHost, "_minio_bucket_2", None)
        endpoint = getattr(TestSelfHost, "_minio_endpoint", None)
        assert all([minio_user, minio_password, bucket2, endpoint])

        # Without confirm_migrate_s3 the API must refuse (409) since we're on s3.
        r = session.post(
            f"{router_url}/api/storage/archive_backend/configure",
            json={
                "s3_bucket": bucket2,
                "s3_access_key_id": minio_user,
                "s3_secret_access_key": minio_password,
                "s3_endpoint": endpoint,
                "s3_region": "us-east-1",
            },
            timeout=60,
        )
        assert r.status_code == 409, f"expected 409 without confirm_migrate_s3, got {r.status_code}: {r.text[:300]}"
        assert "confirm_migrate_s3" in r.text

        # Now migrate for real.
        r = session.post(
            f"{router_url}/api/storage/archive_backend/configure",
            json={
                "s3_bucket": bucket2,
                "s3_access_key_id": minio_user,
                "s3_secret_access_key": minio_password,
                "s3_endpoint": endpoint,
                "s3_region": "us-east-1",
                "confirm_migrate_s3": True,
            },
            timeout=600,
        )
        assert r.status_code == 200, f"s3->s3 configure failed: {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert data.get("backend") == "s3"
        assert data.get("s3_bucket") == bucket2, f"backend not on new bucket: {data}"

        # State reflects the new bucket.
        r = session.get(f"{router_url}/api/storage/archive_backend", timeout=10)
        assert r.json()["s3_bucket"] == bucket2

        # Pre-migration data (seeded in 13f2, already survived local->s3 in 13h2)
        # must ALSO survive s3->s3, byte-identical.
        fb_url = f"https://file-browser.{domain}"
        r = poll_endpoint(
            session,
            f"{fb_url}/{self._PRE_MIGRATION_PATH}",
            timeout=90,
            interval=5,
            fail_msg="pre-migration archive file not readable after s3->s3 migration",
        )
        assert r.text == self._PRE_MIGRATION_CONTENT, "pre-migration archive content changed across s3->s3 migration"

    def test_13j_cleanup_archive_test(self, session, router_url):
        """Remove MinIO and optionally file-browser."""
        # Remove minio
        minio_id = app_id_for(session, router_url, "minio")
        if minio_id:
            session.post(f"{router_url}/remove_app/{minio_id}", timeout=30)
            wait_app_removed(session, router_url, "minio")

        # Only remove file-browser if we deployed it
        if not getattr(TestSelfHost, "_fb_was_preexisting", True):
            fb_id = app_id_for(session, router_url, "file-browser")
            if fb_id:
                session.post(f"{router_url}/remove_app/{fb_id}", timeout=30)
                wait_app_removed(session, router_url, "file-browser")

    # -- 14. Cleanup -------------------------------------------------------

    def test_14_remove_app(self, session, router_url):
        """Remove the deployed test-app."""
        app_id = app_id_for(session, router_url, "test-app")
        r = session.post(f"{router_url}/remove_app/{app_id}", timeout=30)
        assert r.status_code == 202
        wait_app_removed(session, router_url, "test-app")

    def test_14b_app_gone(self, session, domain):
        """After removal, app routes return 404."""
        r = session.get(f"https://test-app.{domain}/health", timeout=5)
        assert r.status_code == 404
