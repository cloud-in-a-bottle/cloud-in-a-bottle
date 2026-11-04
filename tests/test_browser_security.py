import json
from pathlib import Path

import pytest
from playwright.sync_api import Page

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "compute_space/src/compute_space/web/static/js"
ORIGIN = "https://oauth-demo.example.test"


@pytest.fixture
def security_page(page: Page) -> Page:
    # Serve every document locally, including the popup's provider and callback origins.
    page.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<!doctype html>"))
    page.goto(ORIGIN)
    page.add_script_tag(path=SCRIPTS / "navigation.js")
    return page


@pytest.mark.parametrize(
    "destination",
    [
        "javascript:window.redirectExecuted=true",
        " JaVaScRiPt:window.redirectExecuted=true",
        "java\tscript:window.redirectExecuted=true",
        "java\nscript:window.redirectExecuted=true",
        "data:text/html,<script>window.redirectExecuted=true</script>",
        "file:///etc/passwd",
        "https://[invalid",
    ],
)
def test_navigation_rejects_non_http_urls(security_page: Page, destination: str) -> None:
    assert security_page.evaluate("value => navigateToHttpUrl(value)", destination) is False
    assert security_page.url == ORIGIN + "/"
    assert security_page.evaluate("window.redirectExecuted === undefined")


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("/app_detail/demo?tab=logs#latest", ORIGIN + "/app_detail/demo?tab=logs#latest"),
        ("//app.example.test/path", "https://app.example.test/path"),
        ("https://app.example.test/path", "https://app.example.test/path"),
        ("http://app.localhost:8080/path", "http://app.localhost:8080/path"),
    ],
)
def test_navigation_preserves_http_destinations(security_page: Page, destination: str, expected: str) -> None:
    security_page.evaluate("value => navigateToHttpUrl(value)", destination)
    security_page.wait_for_url(expected)


def test_app_ready_redirect_does_not_execute_query_string(security_page: Page) -> None:
    config = {
        "appStatus": "starting",
        "appStatusUrl": "/status",
        "nextUrl": "javascript:window.redirectExecuted=true",
    }
    security_page.set_content(
        '<script id="page-config" type="application/json">' + json.dumps(config) + "</script>"
        '<pre id="app-logs"></pre><span id="app-status"></span>'
    )
    security_page.evaluate("""() => {
        window.WebSocket = function () {};
        window.fetch = async function () {
            return {ok: true, status: 200, json: async () => ({status: 'running'})};
        };
    }""")
    security_page.add_script_tag(path=SCRIPTS / "app-detail.js")
    security_page.wait_for_function("document.getElementById('app-status').textContent === 'running'")
    assert security_page.evaluate("window.redirectExecuted === undefined")
    assert security_page.url == ORIGIN + "/"


def test_oauth_completion_requires_callback_origin_and_opened_popup(security_page: Page) -> None:
    security_page.add_script_tag(path=ROOT / "apps/oauth_demo/src/oauth_demo/static/oauth.js")
    with security_page.expect_popup() as opened:
        security_page.evaluate("""() => {
            window.client = new OAuthClient({
                scopes: {}, appName: 'oauth-demo', zoneDomain: 'example.test'
            });
            window.completed = false;
            client.openPopup('https://provider.example.test/authorize').then(() => {
                window.completed = true;
            });
        }""")
    popup = opened.value
    popup.wait_for_load_state()

    # A message from the right popup while it is still at the provider is untrusted.
    # A different window at the callback origin is untrusted too.
    security_page.evaluate("""() => {
        for (const [origin, source, data] of [
            ['https://provider.example.test', client._popup, {type: 'auth_complete'}],
            [location.origin, window, {type: 'auth_complete'}],
            [location.origin, client._popup, {type: 'unrelated'}],
        ]) {
            window.dispatchEvent(new MessageEvent('message', {origin, source, data}));
        }
    }""")
    assert security_page.evaluate("window.completed") is False

    # The genuine callback can complete authorization without closing the popup.
    popup.goto(ORIGIN + "/client/oauth-complete")
    popup.evaluate("window.opener.postMessage({type: 'auth_complete'}, location.origin)")
    security_page.wait_for_function("window.completed")
    assert not popup.is_closed()
