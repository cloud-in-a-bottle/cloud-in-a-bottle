import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page
from playwright.sync_api import Playwright
from playwright.sync_api import Request
from playwright.sync_api import Route
from playwright.sync_api import expect
from test_accessibility import WCAG_AA_TAGS
from test_accessibility import stack as stack

from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup

EXPORT_PATH = "/api/app-definitions/export"
SECRET = "SYNTHETIC-PRIVATE-SECRET-ONLY"
ERROR = "Could not load app definitions. Reload to try again."


def _export_text(mode: str, secret: str | None = SECRET, missing_secret_keys: tuple[str, ...] = ()) -> str:
    # Non-default indentation, Unicode and a trailing newline detect accidental reserialization.
    data = {
        "schema_version": 1,
        "mode": mode,
        "apps": [
            {
                "name": "synthetic café <img src=x onerror=window.exportXss=true>",
                "source": {"kind": "remote", "repo_url": "https://example.invalid/test.git", "ref": "main"},
                "port_mappings": [{"label": "web", "container_port": 8080, "host_port": 29001}],
                "secret_keys": (["SYNTHETIC_KEY"] if secret is not None else []) + list(missing_secret_keys),
            }
        ],
    }
    if mode == "private":
        data["secret_values"] = {"SYNTHETIC_KEY": secret} if secret is not None else {}
        data["missing_secret_keys"] = list(missing_secret_keys)
    return json.dumps(data, ensure_ascii=False, indent=4) + "\n"


def _fulfill(route: Route, body: str, status: int = 200, content_type: str = "application/json") -> None:
    route.fulfill(status=status, content_type=content_type, headers={"Cache-Control": "no-store"}, body=body)


def _fake_exports(page: Page, missing_secret_keys: tuple[str, ...] = (), secret: str | None = SECRET) -> list[Request]:
    inventory: list[Request] = []

    def respond(route: Route) -> None:
        inventory.append(route.request)
        _fulfill(route, _export_text(route.request.post_data_json["mode"], secret, missing_secret_keys))

    page.route(f"**{EXPORT_PATH}", respond)
    return inventory


def _local_only(route: Route) -> None:
    host = urlsplit(route.request.url).hostname or ""
    if host == "127.0.0.1" or host == "localhost" or host.endswith(".localhost"):
        route.continue_()
    else:
        route.abort()


@pytest.fixture(scope="module")
def export_owner(stack: LocalStack) -> Iterator[requests.Session]:
    # The export UI needs no archive mount, binary download, or host service.
    with sqlite3.connect(stack.config.db_path) as db:
        db.execute("UPDATE archive_backend SET backend = 'disabled' WHERE id = 1")
    with complete_setup(stack) as owner:
        # Stored inventory only: this app is never deployed and has no external provider.
        with sqlite3.connect(stack.config.db_path) as db:
            db.execute(
                "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status)"
                " VALUES ('exportfixture', 'export-fixture', '1.0', '/tmp/export-fixture',"
                " 'https://example.invalid/export-fixture.git#main', 29000, 'stopped')"
            )
            db.execute(
                "INSERT INTO app_port_mappings (app_id, label, container_port, host_port)"
                " VALUES ('exportfixture', 'web', 8080, 29001)"
            )
        yield owner


@pytest.fixture
def export_page(page: Page, stack: LocalStack, export_owner: requests.Session) -> Iterator[Page]:
    page.context.add_cookies(
        [{"name": cookie.name, "value": cookie.value, "url": stack.router_url} for cookie in export_owner.cookies]
    )
    page.route("**/*", _local_only)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    assert not errors, errors


def _open(page: Page, stack: LocalStack) -> None:
    response = page.goto(f"{stack.router_url}/system/")
    assert response is not None and response.ok
    assert page.url == f"{stack.router_url}/system/"


def _ready(page: Page, text: str, status: str = "Ready.") -> None:
    expect(page.locator("#app-definition-status")).to_have_text(status)
    assert page.locator("#app-definition-json").text_content() == text
    expect(page.get_by_role("button", name="Copy", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Download", exact=True)).to_be_enabled()


def _cleared(page: Page, status: str = "Loading…") -> None:
    expect(page.locator("#app-definition-json")).to_be_empty()
    expect(page.get_by_role("button", name="Copy", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Download", exact=True)).to_be_disabled()
    expect(page.locator("#app-definition-status")).to_have_text(status)
    assert SECRET not in page.locator("body").inner_text()


def _download(page: Page, mode: str, text: str) -> None:
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download", exact=True).click()
    download = download_info.value
    assert download.suggested_filename == f"app-definitions-{mode}.json"
    assert Path(download.path()).read_bytes() == text.encode("utf-8")


def _copy(page: Page, text: str) -> None:
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page.locator("#app-definition-status")).to_have_text("Copied.")
    assert page.evaluate("navigator.clipboard.readText()") == text


def test_routed_exports_keyboard_preview_copy_and_download_exact_bytes(
    export_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = export_page
    inventory = _fake_exports(page)
    page.add_init_script("""
      window.exportBlobUrls = new Set();
      const create = URL.createObjectURL.bind(URL);
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = blob => { const url = create(blob); window.exportBlobUrls.add(url); return url; };
      URL.revokeObjectURL = url => { window.exportBlobUrls.delete(url); revoke(url); };
    """)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    expect(page.get_by_role("radio", name="Sharing", exact=True)).to_be_checked()
    expect(page.locator("#app-definition-private-hint")).to_be_hidden()
    assert [request.post_data_json for request in inventory] == [{"mode": "sharing"}]
    assert inventory[0].method == "POST"
    assert inventory[0].headers["accept"] == "application/json"
    assert inventory[0].headers["content-type"] == "application/json"

    summary = page.locator("#app-definition-preview > summary")
    summary.focus()
    summary.press("Enter")
    output = page.get_by_role("region", name="App definitions JSON")
    expect(output).to_be_visible()
    summary.press("Tab")
    expect(output).to_be_focused()
    assert page.locator("#app-definition-json img").count() == 0
    assert page.evaluate("window.exportXss === undefined")
    assert output.get_attribute("aria-live") is None
    page.screenshot(path=str(Path(output_path) / "sharing-expanded.png"), full_page=True)

    for mode in ("sharing", "private"):
        if mode == "private":
            sharing = page.get_by_role("radio", name="Sharing", exact=True)
            sharing.focus()
            sharing.press("ArrowRight")
            expect(page.get_by_role("radio", name="Private (includes secrets)")).to_be_checked()
            _ready(page, _export_text(mode))
            expect(page.locator("#app-definition-private-hint")).to_be_visible()
            expect(output).to_contain_text(SECRET)
            output.evaluate("element => element.scrollTop = element.scrollHeight")
            page.screenshot(path=str(Path(output_path) / "private-expanded.png"), full_page=True)
        _copy(page, _export_text(mode))
        _download(page, mode, _export_text(mode))
        page.wait_for_function("window.exportBlobUrls.size === 0")
    assert [request.post_data_json for request in inventory] == [{"mode": "sharing"}, {"mode": "private"}]
    summary.focus()
    summary.press("Space")
    expect(output).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#app-definition-scope").scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(Path(output_path) / "private-mobile.png"))


@pytest.mark.parametrize(
    ("missing_keys", "secret", "status"),
    [
        (("<img src=x onerror=window.exportXss=true>",), None, "Ready. 1 referenced secret is not configured."),
        (
            ("<img src=x onerror=window.exportXss=true>", "SYNTHETIC_UNCONFIGURED_KEY"),
            SECRET,
            "Ready. 2 referenced secrets are not configured.",
        ),
    ],
    ids=["one-missing-none-configured", "two-missing-some-configured"],
)
def test_routed_private_missing_secrets_remain_ready_with_exact_preview_copy_and_download(
    export_page: Page,
    stack: LocalStack,
    output_path: str,
    missing_keys: tuple[str, ...],
    secret: str | None,
    status: str,
) -> None:
    page = export_page
    inventory = _fake_exports(page, missing_secret_keys=missing_keys, secret=secret)
    _open(page, stack)
    sharing_text = _export_text("sharing", secret=secret, missing_secret_keys=missing_keys)
    _ready(page, sharing_text)
    page.get_by_role("radio", name="Private (includes secrets)").check()
    private_text = _export_text("private", secret=secret, missing_secret_keys=missing_keys)
    _ready(page, private_text, status)
    page.locator("#app-definition-preview > summary").click()
    output = page.get_by_role("region", name="App definitions JSON")
    expect(output).to_be_visible()
    assert json.loads(output.text_content())["missing_secret_keys"] == list(missing_keys)
    assert json.loads(output.text_content())["secret_values"] == (
        {"SYNTHETIC_KEY": secret} if secret is not None else {}
    )
    assert output.locator("img").count() == 0
    assert page.evaluate("window.exportXss === undefined")
    output.evaluate("element => element.scrollTop = element.scrollHeight")
    page.screenshot(path=str(Path(output_path) / "private-missing-secrets.png"), full_page=True)
    _copy(page, private_text)
    _download(page, "private", private_text)
    assert [request.post_data_json for request in inventory] == [{"mode": "sharing"}, {"mode": "private"}]
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _ready(page, sharing_text)


def test_switching_to_sharing_immediately_clears_ready_private_payload(export_page: Page, stack: LocalStack) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _ready(page, _export_text("private"))
    page.locator("#app-definition-preview > summary").click()
    pending: list[Route] = []
    page.route(f"**{EXPORT_PATH}", lambda route: pending.append(route))
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page)
    expect(page.locator("#app-definition-private-hint")).to_be_hidden()
    page.wait_for_function("document.getElementById('app-definition-status').textContent === 'Loading…'")
    assert len(pending) == 1
    _fulfill(pending[0], _export_text("sharing"))
    _ready(page, _export_text("sharing"))


@pytest.mark.parametrize("latest_mode", ["sharing", "private"])
@pytest.mark.parametrize("late_fails", [False, True])
def test_late_private_response_cannot_publish_even_after_returning_to_same_mode(
    export_page: Page, stack: LocalStack, latest_mode: str, late_fails: bool
) -> None:
    page = export_page
    # Deliberately deliver an old HTTP response despite abort, to exercise the generation guard.
    page.add_init_script("""
      const realFetch = window.fetch;
      window.exportSignals = [];
      window.exportCompletions = 0;
      window.fetch = async (url, options) => {
        if (url !== '/api/app-definitions/export') return realFetch(url, options);
        window.exportSignals.push(options.signal);
        const response = await realFetch(url, {...options, signal: undefined});
        await response.clone().text();
        window.exportCompletions++;
        return response;
      };
    """)
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    pending: list[Route] = []
    page.route(f"**{EXPORT_PATH}", lambda route: pending.append(route))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _cleared(page)
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page)
    if latest_mode == "private":
        page.get_by_role("radio", name="Private (includes secrets)").check()
    assert page.evaluate("window.exportSignals[1].aborted")
    assert len(pending) == (3 if latest_mode == "private" else 2)
    latest_text = _export_text(latest_mode, "SYNTHETIC-CURRENT-SECRET")
    _fulfill(pending[-1], latest_text)
    _ready(page, latest_text)
    # Neither a stale success nor a stale error may replace current text or feedback.
    _fulfill(pending[0], _export_text("private"), status=500 if late_fails else 200)
    if latest_mode == "private":
        _fulfill(pending[1], _export_text("sharing"))
    page.wait_for_function("window.exportCompletions === window.exportSignals.length")
    _ready(page, latest_text)
    assert SECRET not in page.locator("#app-definition-json").text_content()
    _copy(page, latest_text)
    _download(page, latest_mode, latest_text)


@pytest.mark.parametrize("latest_fails", [False, True])
def test_private_body_finishing_after_mode_change_cannot_replace_sharing_or_its_error(
    export_page: Page, stack: LocalStack, latest_fails: bool
) -> None:
    page = export_page
    # Return private headers immediately, then hold text() despite cancellation.
    page.add_init_script("""
      const realFetch = window.fetch;
      window.fetch = async (url, options) => {
        const response = await realFetch(url, options);
        if (url === '/api/app-definitions/export' && JSON.parse(options.body).mode === 'private') {
          const text = await response.text();
          response.text = () => new Promise(resolve => { window.releasePrivateBody = () => resolve(text); });
        }
        return response;
      };
    """)
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    page.wait_for_function("typeof window.releasePrivateBody === 'function'")
    _cleared(page)
    if latest_fails:
        page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, "{}", status=500))
    page.get_by_role("radio", name="Sharing", exact=True).check()
    if latest_fails:
        _cleared(page, ERROR)
    else:
        _ready(page, _export_text("sharing"))
    page.evaluate("window.releasePrivateBody()")
    if latest_fails:
        _cleared(page, ERROR)
    else:
        _ready(page, _export_text("sharing"))
        _copy(page, _export_text("sharing"))
        _download(page, "sharing", _export_text("sharing"))


@pytest.mark.parametrize(
    ("body", "http_status", "content_type"),
    [
        (_export_text("sharing"), 500, "application/json"),
        ("<html><h1>Log in</h1></html>", 200, "text/html"),
        ("<html>Log in</html>", 200, "application/json"),
        ("{broken-json", 200, "application/json"),
        ("null", 200, "application/json"),
        (_export_text("private"), 200, "application/json"),
        ('{"schema_version":2,"mode":"sharing","apps":[]}', 200, "application/json"),
        ('{"schema_version":1,"mode":"sharing","apps":{}}', 200, "application/json"),
        ('{"schema_version":1,"mode":"sharing","apps":[null]}', 200, "application/json"),
        ('{"schema_version":1,"mode":"sharing","apps":[],"secret_values":{}}', 200, "application/json"),
        ('{"schema_version":1,"mode":"sharing","apps":[],"missing_secret_keys":[]}', 200, "application/json"),
    ],
    ids=[
        "http-error",
        "login-html",
        "disguised-html",
        "malformed",
        "null",
        "wrong-mode",
        "version",
        "apps",
        "app",
        "secrets",
        "missing-secret-keys",
    ],
)
def test_invalid_response_leaves_no_previous_private_export_actionable(
    export_page: Page, stack: LocalStack, body: str, http_status: int, content_type: str
) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _ready(page, _export_text("private"))
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, body, http_status, content_type))
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page, ERROR)


@pytest.mark.parametrize("values", [None, [], {"SYNTHETIC_KEY": 123}])
def test_private_requires_a_secret_values_string_map(export_page: Page, stack: LocalStack, values: object) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    body = json.dumps(
        {"schema_version": 1, "mode": "private", "apps": [], "secret_values": values, "missing_secret_keys": []}
    )
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, body))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _cleared(page, ERROR)


@pytest.mark.parametrize(
    "field",
    [
        {},
        {"missing_secret_keys": None},
        {"missing_secret_keys": "SYNTHETIC_KEY"},
        {"missing_secret_keys": {}},
        {"missing_secret_keys": [None]},
        {"missing_secret_keys": [123]},
        {"missing_secret_keys": ["SYNTHETIC_KEY", 123]},
    ],
    ids=["omitted", "null", "string", "object", "null-key", "numeric-key", "mixed-keys"],
)
def test_private_requires_missing_secret_keys_as_a_string_list(
    export_page: Page, stack: LocalStack, field: dict[str, object]
) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    data = {"schema_version": 1, "mode": "private", "apps": [], "secret_values": {}, **field}
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, json.dumps(data)))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _cleared(page, ERROR)


@pytest.mark.parametrize("outcome", ["resolve", "reject"])
def test_clipboard_completion_after_mode_change_cannot_fallback_or_change_feedback(
    export_page: Page, stack: LocalStack, outcome: str
) -> None:
    page = export_page
    page.add_init_script("""
      window.fallbackCopies = [];
      Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => new Promise((resolve, reject) => {
        window.finishCopy = {resolve, reject};
      })}});
      document.execCommand = () => { window.fallbackCopies.push(document.activeElement.value); return true; };
    """)
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _ready(page, _export_text("private"))
    page.get_by_role("button", name="Copy", exact=True).click()
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _ready(page, _export_text("sharing"))
    page.evaluate("outcome => window.finishCopy[outcome]()", outcome)
    assert page.evaluate("window.fallbackCopies") == []
    _ready(page, _export_text("sharing"))


@pytest.mark.parametrize("clipboard_available", [False, True])
def test_clipboard_fallback_copies_exact_text_and_restores_keyboard_focus(
    export_page: Page, stack: LocalStack, clipboard_available: bool
) -> None:
    page = export_page
    page.add_init_script("""
      window.fallbackCopies = [];
      document.execCommand = () => { window.fallbackCopies.push(document.activeElement.value); return true; };
    """)
    clipboard = (
        "{writeText: () => Promise.reject(new Error('test rejection'))}" if clipboard_available else "undefined"
    )
    page.add_init_script(f"Object.defineProperty(navigator, 'clipboard', {{value: {clipboard}}});")
    inventory = _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    button = page.get_by_role("button", name="Copy", exact=True)
    button.focus()
    button.press("Enter")
    expect(page.locator("#app-definition-status")).to_have_text("Copied.")
    expect(button).to_be_focused()
    assert page.evaluate("window.fallbackCopies") == [_export_text("sharing")]
    assert page.locator("textarea").count() == 0
    assert len(inventory) == 1


def test_reload_back_and_return_to_system_request_only_sharing(export_page: Page, stack: LocalStack) -> None:
    page = export_page
    inventory = _fake_exports(page)
    _open(page, stack)
    for navigation in ("reload", "back", "return"):
        _ready(page, _export_text("sharing"))
        page.get_by_role("radio", name="Private (includes secrets)").check()
        _ready(page, _export_text("private"))
        count = len(inventory)
        if navigation == "reload":
            page.reload()
        else:
            page.goto(f"{stack.router_url}/dashboard")
            if navigation == "back":
                page.go_back()
            else:
                page.goto(f"{stack.router_url}/system/")
        _ready(page, _export_text("sharing"))
        expect(page.get_by_role("radio", name="Sharing", exact=True)).to_be_checked()
        expect(page.get_by_role("radio", name="Private (includes secrets)")).not_to_be_checked()
        expect(page.locator("#app-definition-private-hint")).to_be_hidden()
        assert page.locator("#app-definition-preview").get_attribute("open") is None
        assert [request.post_data_json for request in inventory[count:]] == [{"mode": "sharing"}]


def test_export_expanded_private_loading_and_error_states_have_no_wcag_aa_violations(
    export_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = export_page
    axe = Axe()
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.locator("#app-definition-preview > summary").click()

    def scan(state: str) -> None:
        results = axe.run(page, options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}})
        assert not results.response["violations"], f"{state}: {results.response['violations']}"
        page.screenshot(path=str(Path(output_path) / f"{state}.png"), full_page=True)

    scan("sharing-expanded")
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _ready(page, _export_text("private"))
    scan("private-expanded")
    pending: list[Route] = []
    page.route(f"**{EXPORT_PATH}", lambda route: pending.append(route))
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page)
    scan("loading")
    assert len(pending) == 1
    _fulfill(pending[0], "<html>Log in</html>", content_type="text/html")
    _cleared(page, ERROR)
    scan("error")
    missing_text = _export_text("private", missing_secret_keys=("SYNTHETIC_MISSING_ONE", "SYNTHETIC_MISSING_TWO"))
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, missing_text))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _ready(page, missing_text, "Ready. 2 referenced secrets are not configured.")
    scan("private-missing-secrets")


def test_real_owner_endpoint_exports_stored_inventory(export_page: Page, stack: LocalStack) -> None:
    page = export_page
    # No export route mock: exercise the current checkout's authenticated owner endpoint.
    with page.expect_response(f"**{EXPORT_PATH}") as response_info:
        _open(page, stack)
    response = response_info.value
    assert response.ok
    assert response.headers["cache-control"] == "no-store"
    sharing = response.text()
    _ready(page, sharing)
    apps = response.json()["apps"]
    assert len(apps) == 1
    assert apps[0]["name"] == "export-fixture"
    assert apps[0]["port_mappings"] == [{"label": "web", "container_port": 8080, "host_port": 29001}]
    assert "secret_values" not in response.json()
    assert "missing_secret_keys" not in response.json()
    _download(page, "sharing", sharing)
    with page.expect_response(f"**{EXPORT_PATH}") as response_info:
        page.get_by_role("radio", name="Private (includes secrets)").check()
    response = response_info.value
    assert response.ok
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["secret_values"] == {}
    assert response.json()["missing_secret_keys"] == []
    _ready(page, response.text())
    _download(page, "private", response.text())


def test_actual_bfcache_restoration_resets_payload_model_and_restored_radios(
    playwright: Playwright, stack: LocalStack, export_owner: requests.Session
) -> None:
    # Playwright disables BFCache by default. Enable real history caching, not synthetic events.
    browser = playwright.chromium.launch(
        channel="chromium",
        ignore_default_args=["--disable-back-forward-cache"],
        proxy={"server": "http://127.0.0.1:9", "bypass": "*.localhost,localhost,127.0.0.1"},
    )
    try:
        context = browser.new_context()
        context.add_cookies(
            [{"name": cookie.name, "value": cookie.value, "url": stack.router_url} for cookie in export_owner.cookies]
        )
        # Request routing disables Chromium's cache. Block external assets using the proxy instead.
        page = context.new_page()
        inventory: list[str] = []
        page.on(
            "request",
            lambda request: inventory.append(request.post_data or "") if request.url.endswith(EXPORT_PATH) else None,
        )
        _open(page, stack)
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        page.get_by_role("radio", name="Private (includes secrets)").check()
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        page.locator("#app-definition-preview > summary").click()
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
          window.bfcacheShows = [];
          window.addEventListener('pagehide', () => {
            window.exportClearedOnLeave = document.getElementById('app-definition-json').textContent === ''
              && document.getElementById('app-definition-copy').disabled
              && document.getElementById('app-definition-download').disabled;
          });
          window.addEventListener('pageshow', event => {
            window.bfcacheShows.push(event.persisted);
            // Model the late native form restoration phase, after the application's pageshow handler.
            document.querySelector('input[name="app-definition-mode"][value="private"]').checked = true;
          });
        }""")
        count = len(inventory)
        page.goto(f"{stack.router_url}/dashboard")
        # A BFCache restore commits a history entry without firing a new load event.
        page.go_back(wait_until="commit")
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        assert page.evaluate("window.bfcacheShows") == [True], page.evaluate(
            "JSON.stringify(performance.getEntriesByType('navigation')[0].notRestoredReasons?.toJSON())"
        )
        assert page.evaluate("window.exportClearedOnLeave")
        expect(page.get_by_role("radio", name="Sharing", exact=True)).to_be_checked()
        expect(page.get_by_role("radio", name="Private (includes secrets)")).not_to_be_checked()
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        assert json.loads(page.locator("#app-definition-json").text_content())["mode"] == "sharing"
        assert [json.loads(body) for body in inventory[count:]] == [{"mode": "sharing"}]
        assert page.locator("#app-definition-preview").get_attribute("open") is None
    finally:
        browser.close()
