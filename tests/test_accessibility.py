"""Automated WCAG checks for representative Cloud in a Bottle UI pages."""

import socket
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import expect

from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.utils import managed_router

WCAG_AA_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"]
PUBLIC_PAGES = ["/setup"]
AUTHENTICATED_PAGES = [
    "/dashboard",
    "/add_app",
    "/settings",
    "/system/",
    "/diagnostics/",
    "/terminal/",
    "/docs/",
]


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalStack]:
    config = make_local_stack_config(
        data_root_dir=str(tmp_path_factory.mktemp("accessibility")),
        port=_unused_port(),
        zone_name="accessibility",
        default_apps=[],
    )
    local_stack = LocalStack(config=config)
    with managed_router(config):
        yield local_stack


def _scan_page(page: Page, axe: Axe, base_url: str, path: str) -> list[str]:
    target_url = f"{base_url}{path}"
    response = page.goto(target_url, wait_until="load")
    assert response is not None and response.ok, f"{path} returned {response.status if response else 'no response'}"
    assert page.url == target_url, f"{path} redirected to {page.url}"

    if path == "/settings":
        page.wait_for_function(
            """() => !document.getElementById('archive-backend-status').textContent.trim().startsWith('Loading')
              && document.querySelector('select[data-service="github.com/example/a11y"]')
              && document.getElementById('ssh-status').textContent.trim()"""
        )
    elif path == "/system/":
        page.wait_for_function(
            """() => ['storage-usage-chart', 'cpu-usage-chart', 'mem-usage-chart',
                       'swap-usage-chart', 'storage-body', 'ports-body', 'cs-logs']
              .every(id => {
                const text = document.getElementById(id).textContent.trim();
                return text && !text.startsWith('Loading');
              })"""
        )
    elif path == "/diagnostics/":
        page.wait_for_function("() => !document.getElementById('diag-json').textContent.startsWith('Loading')")
    elif path == "/terminal/":
        page.locator(".xterm-screen").wait_for()

    results = axe.run(
        page,
        options={
            "runOnly": {"type": "tag", "values": WCAG_AA_TAGS},
            "resultTypes": ["violations"],
        },
    )
    failures = []
    for violation in results.response["violations"]:
        for node in violation["nodes"]:
            targets = ", ".join(str(target) for target in node["target"])
            summary = node.get("failureSummary", "").replace("\n", " ")
            failures.append(
                f"{path}: {violation['id']} ({violation['impact']}) at {targets}: {violation['help']}. {summary}"
            )
    return failures


def _seed_service_provider(stack: LocalStack) -> None:
    with sqlite3.connect(stack.config.db_path) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('a11yprovider', 'a11y-provider', '1.0', '/tmp/a11y-provider', 29999, 'running')"
        )
        db.execute(
            "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint)"
            " VALUES ('github.com/example/a11y', 'a11yprovider', '1.0', '/')"
        )


def test_owner_ui_has_no_automatically_detectable_wcag_2_2_aa_violations(page: Page, stack: LocalStack) -> None:
    axe = Axe()
    failures = []

    for path in PUBLIC_PAGES:
        failures.extend(_scan_page(page, axe, stack.router_url, path))

    owner = complete_setup(stack)
    _seed_service_provider(stack)
    failures.extend(_scan_page(page, axe, stack.router_url, "/login"))
    page.context.add_cookies(
        [{"name": cookie.name, "value": cookie.value, "url": stack.router_url} for cookie in owner.cookies]
    )

    for path in AUTHENTICATED_PAGES:
        failures.extend(_scan_page(page, axe, stack.router_url, path))

    assert not failures, "Automatically detectable WCAG 2.2 AA violations:\n" + "\n".join(failures)


@pytest.fixture
def app_backend() -> Iterator[tuple[int, list[str]]]:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            body = b"<!doctype html><html lang='en'><title>Ready</title><h1>App is ready</h1></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(("initial_status", "width"), [("building", 1280), ("starting", 390)])
def test_app_launch_waits_at_its_own_url_then_opens_when_ready(
    page: Page,
    browser: Browser,
    stack: LocalStack,
    app_backend: tuple[int, list[str]],
    initial_status: str,
    width: int,
) -> None:
    backend_port, backend_paths = app_backend
    owner = complete_setup(stack)
    with closing(sqlite3.connect(stack.config.db_path)) as db, db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('startingtest', 'startup-test', '1.0', '/tmp/startup-test', ?, ?)",
            (backend_port, initial_status),
        )

    def set_state(status: str, port: int = backend_port) -> None:
        with closing(sqlite3.connect(stack.config.db_path)) as db, db:
            db.execute("UPDATE apps SET status = ?, local_port = ? WHERE app_id = 'startingtest'", (status, port))

    # Keep the real zone-wide cookie scope so private app subdomains are authenticated.
    page.context.add_cookies(
        [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in owner.cookies]
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{stack.router_url}/dashboard")
    row = page.locator('[data-app-id="startingtest"]')
    launch = row.locator(".app-row__name")
    app_url = stack.app_url("startup-test") + "/"
    expect(launch).to_be_enabled()
    expect(launch).to_have_attribute("href", app_url)
    expect(row.locator(".app-row__status")).to_have_class("visually-hidden app-row__status")
    with page.expect_popup() as popup:
        launch.click()
    app_page = popup.value
    expect(app_page.get_by_role("heading", name="Your app is coming up")).to_be_visible()
    expect(app_page.locator("main button, main a")).to_have_count(0)
    assert app_page.url == app_url
    assert backend_paths == []
    app_page.close()

    # The post-deploy Details page offers the same app URL and waiting experience.
    row.hover()
    row.get_by_role("link", name="Details").click()
    page.wait_for_url(f"{stack.router_url}/app_detail/startup-test")
    with page.expect_popup() as detail_popup:
        page.locator(f'a[href="{app_url}"]').first.click()
    waiting = detail_popup.value
    waiting.set_viewport_size({"width": width, "height": 900})
    retried = waiting.wait_for_event(
        "response", predicate=lambda r: r.url == app_url and r.request.is_navigation_request(), timeout=10000
    )
    assert retried.status == 503
    assert backend_paths == []
    with browser.new_context(
        java_script_enabled=False, viewport={"width": width, "height": 900}, storage_state=page.context.storage_state()
    ) as plain_context:
        plain = plain_context.new_page()
        plain.goto(app_url)
        expect(plain.get_by_role("heading", name="Your app is coming up")).to_be_visible()
        expect(plain.locator("main button, main a")).to_have_count(0)
        expect(plain.locator("#startup-hint")).to_contain_text("refresh this page")
        assert plain.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert plain.locator(".panel").evaluate("element => getComputedStyle(element).borderTopStyle") == "solid"
    with browser.new_context(
        viewport={"width": width, "height": 900}, storage_state=page.context.storage_state()
    ) as audit_context:
        audited = audit_context.new_page()
        # Keep this static scan from navigating away; real automatic retries are
        # exercised by the separate waiting tab. Axe itself needs JavaScript.
        audited.route(
            "**/static/js/app-starting.js*",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=""),
        )
        audited.goto(app_url)
        audit = Axe().run(audited, options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}})
        assert not audit.response["violations"], audit.response["violations"]

    set_state("running")
    expect(waiting.get_by_role("heading", name="App is ready")).to_be_visible(timeout=15000)
    assert waiting.url == app_url
    assert backend_paths.count("/") == 1
    assert waiting.evaluate("window.opener") is None

    # Direct links preserve encoded paths, repeated query fields, and the fragment.
    set_state("starting")
    raw_target = "deep/a%2Fb?tag=one&tag=two&next=%2Fprivate"
    deep_url = app_url + raw_target + "#keep-this"
    response = waiting.goto(deep_url)
    assert response is not None and response.status == 503
    expect(waiting.locator("main button, main a")).to_have_count(0)
    set_state("running")
    expect(waiting.get_by_role("heading", name="App is ready")).to_be_visible(timeout=15000)
    assert waiting.url == deep_url
    assert backend_paths.count("/" + raw_target) == 1

    # Leaving startup for an actual failure must end the waiting/retry page.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        set_state("starting", unavailable.getsockname()[1])
        response = waiting.goto(app_url + "failed?view=logs#keep-this")
        assert response is not None and response.status == 503
        set_state("error", unavailable.getsockname()[1])
        expect(waiting.locator("body")).to_have_text("App is not responding", timeout=15000)
        waiting.evaluate("window.testDocumentMarker = 'failed'")
        waiting.wait_for_timeout(3500)
        assert waiting.evaluate("window.testDocumentMarker") == "failed"
    waiting.close()
