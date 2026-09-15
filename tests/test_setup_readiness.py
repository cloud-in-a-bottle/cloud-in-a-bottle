"""Run the setup route's actual success document in Chromium, without restarting a router."""

import json
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock

import httpx
import pytest
from litestar.testing import TestClient
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect

from compute_space import config as config_module
from compute_space.config import DefaultConfig
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.db import connection
from compute_space.web import setup_app

ORIGIN = "http://readiness.localhost"
STARTING = "Starting…"
DEADLINE_MESSAGE = "Taking longer than expected."
CLOCK_START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def setup_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    """A real setup POST, including DB/session creation; only deployment/restart are stubbed."""
    config = DefaultConfig(data_root_dir=str(tmp_path), claim_token_required=False, default_apps=[])
    config.make_all_dirs()
    monkeypatch.setattr(config_module, "_active_config", config)
    monkeypatch.setattr(connection, "_db_path", None)  # Restore any previous app's DB after this test.
    connection.init_db(config.db_path)
    with closing(connection.get_db()) as db:
        seed_domains(db, Domain(name="readiness.localhost", tls=False), [])
    monkeypatch.setattr(setup_app, "_setup_completed", False)
    monkeypatch.setattr(setup_app, "is_shutdown_pending", lambda: False)
    monkeypatch.setattr(setup_app, "deploy_default_apps", Mock())
    restart = AsyncMock()
    monkeypatch.setattr(setup_app, "_trigger_restart_after_response", restart)

    with TestClient(setup_app.create_setup_app(config), base_url=ORIGIN) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/setup", data={"password": "test-password", "confirm_password": "test-password"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "session_token" in response.cookies
        restart.assert_awaited_once()
        # The still-listening setup app must not masquerade as the ready dashboard.
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json() == {"status": "restarting"}
    return response


@pytest.fixture
def navigation_requests(page: Page) -> list[tuple[str, str]]:
    requests = []
    page.on(
        "request",
        lambda request: requests.append((request.method, request.url)) if request.is_navigation_request() else None,
    )
    return requests


@pytest.fixture
def setup_page(page: Page, setup_response: httpx.Response, navigation_requests: list[tuple[str, str]]) -> Page:
    def serve(route: Route) -> None:
        if route.request.url == f"{ORIGIN}/setup":
            if route.request.method == "POST":
                route.fulfill(status=200, headers=dict(setup_response.headers), body=setup_response.content)
            else:
                # Use a browser form POST so navigation assertions can detect POST replay.
                route.fulfill(
                    content_type="text/html",
                    body="<form method='post'><button>Complete setup</button></form>",
                )
        elif route.request.url == f"{ORIGIN}/":
            assert "session_token=" in route.request.all_headers().get("cookie", "")
            route.fulfill(content_type="text/html", body="<h1>Dashboard</h1>")
        else:
            # The success document must work even while static assets are unavailable.
            route.abort("connectionrefused")

    page.route("**/*", serve)
    # Observe real fetches and their aborts without supplying readiness decisions.
    page.add_init_script("""(() => {
        window.healthChecks = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (input, options) => {
            const request = new Request(input, options);
            if (new URL(request.url).pathname !== '/health') return originalFetch(input, options);
            const check = {cache: request.cache, started: Date.now(), aborted: false,
                           finished: false, bodyStarted: false, bodyFinished: false,
                           hasSignal: options?.signal instanceof AbortSignal};
            window.healthChecks.push(check);
            request.signal.addEventListener('abort', () => { check.aborted = true; });
            try {
                const response = await originalFetch(input, options);
                const originalJson = response.json.bind(response);
                response.json = async () => {
                    check.bodyStarted = true;
                    try { return await originalJson(); }
                    catch (error) { check.bodyError = error.name; throw error; }
                    finally { check.bodyFinished = true; }
                };
                return response;
            } finally { check.finished = true; }
        };
    })();""")
    return page


@pytest.fixture
def health_routes(setup_page: Page) -> list[Route]:
    routes = []
    setup_page.route(f"{ORIGIN}/health", lambda route: routes.append(route))
    return routes


def _submit(page: Page, *, javascript: bool = True) -> None:
    if javascript:
        # Leave room for CI scheduling delays between installing and pausing the running clock.
        page.clock.install(time=CLOCK_START - timedelta(days=1))
        page.clock.pause_at(CLOCK_START)
    page.goto(f"{ORIGIN}/setup")
    page.get_by_role("button", name="Complete setup").click()
    expect(page.get_by_role("heading", name="Setup complete", exact=True)).to_be_visible()
    expect(page.locator("p#setup-status")).to_have_attribute("role", "status")
    expect(page.get_by_role("status")).to_have_text(STARTING)
    expect(page.get_by_role("link", name="Open dashboard", exact=True)).to_have_attribute("href", "/")


def _wait_for_check(page: Page, index: int, field: str = "finished") -> None:
    # Browser network/body delivery is real even when its JS clock is paused.
    page.wait_for_function("([index, field]) => window.healthChecks[index]?.[field]", arg=[index, field])


def _retry(page: Page, routes: list[Route]) -> Route:
    count = len(routes)
    with page.expect_request(f"{ORIGIN}/health"):
        page.clock.run_for(1_000)
    page.wait_for_function("count => window.healthChecks.length > count", arg=count)
    assert len(routes) == count + 1
    return routes[-1]


def _assert_waiting(page: Page, navigation_requests: list[tuple[str, str]]) -> None:
    expect(page).to_have_url(f"{ORIGIN}/setup")
    expect(page.get_by_role("status")).to_have_text(STARTING)
    assert navigation_requests == [("GET", f"{ORIGIN}/setup"), ("POST", f"{ORIGIN}/setup")]


def test_immediately_healthy_replaces_setup_history_with_dashboard(
    setup_page: Page, health_routes: list[Route], navigation_requests: list[tuple[str, str]]
) -> None:
    _submit(setup_page)
    history_length = setup_page.evaluate("history.length")
    assert len(health_routes) == 1
    assert setup_page.evaluate("healthChecks[0].cache") == "no-store"
    assert setup_page.evaluate("healthChecks[0].hasSignal") is True
    health_routes[0].fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")
    expect(setup_page.get_by_role("heading", name="Dashboard")).to_be_visible()
    assert len(health_routes) == 1  # No requirement to observe downtime first.
    assert setup_page.evaluate("history.length") == history_length
    assert navigation_requests[-1] == ("GET", f"{ORIGIN}/")


@pytest.mark.parametrize("width", [390, 1280], ids=["mobile", "desktop"])
def test_waiting_page_keeps_brand_styling_without_static_assets(
    setup_page: Page, health_routes: list[Route], width: int
) -> None:
    setup_page.set_viewport_size({"width": width, "height": 720})
    _submit(setup_page)
    expect(setup_page.get_by_role("heading", name="Cloud in a Bottle", exact=True)).to_be_visible()
    expect(setup_page.locator(".panel")).to_have_css("background-color", "rgb(252, 252, 252)")
    expect(setup_page.get_by_role("link", name="Open dashboard")).to_have_css("background-color", "rgb(162, 217, 255)")
    for selector in (".cloud--1", ".deco-grass"):
        decoration = setup_page.locator(selector)
        expect(decoration).to_be_visible()
        assert "data:image/svg+xml;base64," in decoration.evaluate("el => getComputedStyle(el).backgroundImage")
    assert setup_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert len(health_routes) == 1
    health_routes[0].fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


def test_slow_optional_font_does_not_block_readiness(setup_page: Page, health_routes: list[Route]) -> None:
    font_requests = []
    setup_page.route("https://fonts.googleapis.com/**", lambda route: font_requests.append(route))
    setup_page.goto(f"{ORIGIN}/setup")
    with setup_page.expect_request("https://fonts.googleapis.com/**"), setup_page.expect_request(f"{ORIGIN}/health"):
        setup_page.locator("form").evaluate("form => form.requestSubmit()")
    expect(setup_page.get_by_role("status")).to_have_text(STARTING)
    assert font_requests  # Keep the stylesheet pending throughout recovery.
    health_routes[0].fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


def test_waits_through_503_and_connection_refusal_for_more_than_two_seconds(
    setup_page: Page, health_routes: list[Route], navigation_requests: list[tuple[str, str]]
) -> None:
    _submit(setup_page)
    for index in range(4):
        route = health_routes[0] if index == 0 else _retry(setup_page, health_routes)
        if index % 2:
            route.abort("connectionrefused")
        else:
            route.fulfill(status=503, json={"status": "restarting"})
        _wait_for_check(setup_page, index)
        _assert_waiting(setup_page, navigation_requests)
    checks = setup_page.evaluate("healthChecks")
    assert checks[-1]["started"] - checks[0]["started"] == 3_000
    assert all(check["cache"] == "no-store" for check in checks)
    _retry(setup_page, health_routes).fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


def test_hung_request_is_aborted_after_three_seconds_then_retried(
    setup_page: Page, health_routes: list[Route], navigation_requests: list[tuple[str, str]]
) -> None:
    _submit(setup_page)
    setup_page.clock.run_for(2_999)
    assert setup_page.evaluate("healthChecks[0].aborted") is False
    assert len(health_routes) == 1  # No overlapping polls.
    _assert_waiting(setup_page, navigation_requests)
    setup_page.clock.run_for(1)
    _wait_for_check(setup_page, 0, "aborted")
    _wait_for_check(setup_page, 0)
    _retry(setup_page, health_routes).fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


@pytest.fixture
def health_response_server(setup_page: Page) -> Iterator[str]:
    """Real streaming/redirect targets: fulfill() cannot stream or intercept redirect hops."""
    # A route-fulfilled document has no loopback address space; allow its real test server.
    setup_page.context.grant_permissions(["local-network-access"], origin=ORIGIN)
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"status":"ok"}' if self.path == "/healthy" else b'{"status":'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)) if self.path == "/healthy" else "100")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            if self.path != "/healthy":
                stop.wait()

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            stop.set()
            server.shutdown()
            thread.join(timeout=5)


def test_hung_json_body_is_also_aborted_and_retried(
    setup_page: Page,
    health_routes: list[Route],
    health_response_server: str,
    navigation_requests: list[tuple[str, str]],
) -> None:
    _submit(setup_page)
    health_routes[0].continue_(url=f"{health_response_server}/partial")
    _wait_for_check(setup_page, 0, "bodyStarted")  # HTTP 200 headers arrived and response.json() was called.
    setup_page.clock.run_for(2_999)
    assert setup_page.evaluate("healthChecks[0].aborted") is False
    assert setup_page.evaluate("healthChecks[0].bodyFinished") is False
    assert len(health_routes) == 1
    _assert_waiting(setup_page, navigation_requests)
    setup_page.clock.run_for(1)
    _wait_for_check(setup_page, 0, "aborted")
    _wait_for_check(setup_page, 0, "bodyFinished")
    assert setup_page.evaluate("healthChecks[0].bodyError") == "AbortError"
    _retry(setup_page, health_routes).fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(200, "not JSON", id="malformed-json"),
        pytest.param(200, json.dumps({"status": "starting"}), id="not-ready"),
        pytest.param(200, json.dumps({"status": True}), id="non-string-status"),
        pytest.param(200, "{}", id="missing-status"),
        pytest.param(200, "null", id="null"),
        pytest.param(201, json.dumps({"status": "ok"}), id="wrong-http-success-status"),
        pytest.param(503, json.dumps({"status": "ok"}), id="failed-http-status"),
    ],
)
def test_only_http_200_with_ok_json_is_ready(
    setup_page: Page,
    health_routes: list[Route],
    navigation_requests: list[tuple[str, str]],
    status: int,
    body: str,
) -> None:
    _submit(setup_page)
    health_routes[0].fulfill(status=status, content_type="application/json", body=body)
    _wait_for_check(setup_page, 0, "bodyFinished" if status == 200 else "finished")
    route = _retry(setup_page, health_routes)
    _assert_waiting(setup_page, navigation_requests)
    route.fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


def test_redirect_is_not_readiness(
    setup_page: Page,
    health_routes: list[Route],
    navigation_requests: list[tuple[str, str]],
    health_response_server: str,
) -> None:
    _submit(setup_page)
    # A followed redirect would really return HTTP 200 + ok, rather than a mock/DNS failure.
    health_routes[0].fulfill(status=302, headers={"Location": f"{health_response_server}/healthy"})
    _wait_for_check(setup_page, 0)
    route = _retry(setup_page, health_routes)
    _assert_waiting(setup_page, navigation_requests)
    route.fulfill(json={"status": "ok"})
    expect(setup_page).to_have_url(f"{ORIGIN}/")


def test_deadline_bounds_last_request_stops_polling_and_keeps_manual_get_link(
    setup_page: Page, health_routes: list[Route], navigation_requests: list[tuple[str, str]]
) -> None:
    _submit(setup_page)
    health_routes[0].fulfill(status=503, json={"status": "restarting"})
    _wait_for_check(setup_page, 0)
    # Simulate a suspended tab waking near the deadline. Its next request has only 2s left.
    with setup_page.expect_request(f"{ORIGIN}/health"):
        setup_page.clock.fast_forward(118_000)
    setup_page.clock.run_for(1_999)
    _assert_waiting(setup_page, navigation_requests)
    assert setup_page.evaluate("healthChecks[1].aborted") is False
    setup_page.clock.run_for(1)
    expect(setup_page.get_by_role("status")).to_have_text(DEADLINE_MESSAGE)
    _wait_for_check(setup_page, 1, "aborted")
    _wait_for_check(setup_page, 1)
    count = len(health_routes)
    setup_page.clock.run_for(120_000)
    assert len(health_routes) == count
    expect(setup_page).to_have_url(f"{ORIGIN}/setup")
    link = setup_page.get_by_role("link", name="Open dashboard", exact=True)
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", "/")
    link.click()
    expect(setup_page).to_have_url(f"{ORIGIN}/")
    assert navigation_requests == [
        ("GET", f"{ORIGIN}/setup"),
        ("POST", f"{ORIGIN}/setup"),
        ("GET", f"{ORIGIN}/"),
    ]


@pytest.mark.browser_context_args(java_script_enabled=False)
def test_no_javascript_has_manual_get_link(
    setup_page: Page, health_routes: list[Route], navigation_requests: list[tuple[str, str]]
) -> None:
    _submit(setup_page, javascript=False)
    assert not health_routes
    setup_page.get_by_role("link", name="Open dashboard", exact=True).click()
    expect(setup_page).to_have_url(f"{ORIGIN}/")
    assert navigation_requests == [
        ("GET", f"{ORIGIN}/setup"),
        ("POST", f"{ORIGIN}/setup"),
        ("GET", f"{ORIGIN}/"),
    ]
