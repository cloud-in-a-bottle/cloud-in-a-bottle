import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
import yaml
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
RAW_KEY = "SYNTHETIC-ORIGINAL-KEY-NEVER-EXPORTED"
TOKEN_HASH = hashlib.sha256(RAW_KEY.encode()).hexdigest()
PRIVATE_LABEL = "Private (includes API-token records)"
ERROR = "Could not load app definitions. Reload to try again."


def _export_text(mode: str, token_hash: str | None = TOKEN_HASH) -> str:
    # Comments, explicit document markers, indentation, Unicode and final newlines must survive the UI.
    data = {
        "schema_version": 2,
        "mode": mode,
        "apps": [
            {
                "name": "synthetic café <img src=x onerror=window.exportXss=true>",
                "source": {"kind": "remote", "repo_url": "https://example.invalid/test.git", "ref": "main"},
                "port_mappings": [{"label": "web", "container_port": 8080, "host_port": 29001}],
            }
        ],
    }
    if mode == "private":
        data["platform_api_tokens"] = (
            [
                {
                    "name": "synthetic <img src=x onerror=window.exportXss=true>",
                    "token_hash": token_hash,
                    "expires_at": None,
                }
            ]
            if token_hash is not None
            else []
        )
    return (
        "# Server-formatted YAML: café\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=4, explicit_start=True, explicit_end=True)
        + "\n"
    )


def _export_headers(mode: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-App-Definitions-Mode": mode,
        "X-App-Definitions-Schema-Version": "2",
    }


def _fulfill(
    route: Route,
    body: str,
    status: int = 200,
    content_type: str = "application/yaml",
    *,
    headers: dict[str, str | None] | None = None,
) -> None:
    metadata = _export_headers(route.request.post_data_json["mode"])
    for name, value in (headers or {}).items():
        if value is None:
            metadata.pop(name, None)
        else:
            metadata[name] = value
    route.fulfill(status=status, content_type=content_type, headers=metadata, body=body)


def _fake_exports(page: Page, token_hash: str | None = TOKEN_HASH) -> list[Request]:
    inventory: list[Request] = []

    def respond(route: Route) -> None:
        inventory.append(route.request)
        mode = route.request.post_data_json["mode"]
        _fulfill(route, _export_text(mode, token_hash))

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
            db.execute(
                "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES ('browser token', ?, '')", (TOKEN_HASH,)
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
    assert page.locator("#app-definition-output").text_content() == text
    expect(page.get_by_role("button", name="Copy", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Download", exact=True)).to_be_enabled()


def _cleared(page: Page, status: str = "Loading…") -> None:
    expect(page.locator("#app-definition-output")).to_be_empty()
    expect(page.get_by_role("button", name="Copy", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Download", exact=True)).to_be_disabled()
    expect(page.locator("#app-definition-status")).to_have_text(status)
    assert TOKEN_HASH not in page.locator("body").inner_text()
    assert RAW_KEY not in page.locator("body").inner_text()


def _download(page: Page, mode: str, text: str) -> None:
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download", exact=True).click()
    download = download_info.value
    assert download.suggested_filename == f"app-definitions-{mode}.yaml"
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
      window.exportBlobTypes = [];
      const create = URL.createObjectURL.bind(URL);
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = blob => {
        window.exportBlobTypes.push(blob.type);
        const url = create(blob); window.exportBlobUrls.add(url); return url;
      };
      URL.revokeObjectURL = url => { window.exportBlobUrls.delete(url); revoke(url); };
    """)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    expect(page.get_by_role("radio", name="Sharing", exact=True)).to_be_checked()
    expect(page.locator("#app-definition-private-hint")).to_be_hidden()
    assert [request.post_data_json for request in inventory] == [{"mode": "sharing"}]
    assert inventory[0].method == "POST"
    assert inventory[0].headers["accept"] == "application/yaml"
    assert inventory[0].headers["content-type"] == "application/json"

    summary = page.locator("#app-definition-preview > summary")
    summary.focus()
    summary.press("Enter")
    output = page.get_by_role("region", name="App definitions YAML")
    expect(output).to_be_visible()
    summary.press("Tab")
    expect(output).to_be_focused()
    assert page.locator("#app-definition-output img").count() == 0
    assert page.evaluate("window.exportXss === undefined")
    assert output.get_attribute("aria-live") is None
    page.screenshot(path=str(Path(output_path) / "sharing-expanded.png"), full_page=True)

    for mode in ("sharing", "private"):
        if mode == "private":
            sharing = page.get_by_role("radio", name="Sharing", exact=True)
            sharing.focus()
            sharing.press("ArrowRight")
            expect(page.get_by_role("radio", name=PRIVATE_LABEL)).to_be_checked()
            _ready(page, _export_text(mode))
            expect(page.locator("#app-definition-private-hint")).to_be_visible()
            expect(output).to_contain_text(TOKEN_HASH)
            expect(page.locator("#app-definition-private-hint")).to_contain_text(
                "Original key strings are not available."
            )
            assert RAW_KEY not in output.text_content()
            output.evaluate("element => element.scrollTop = element.scrollHeight")
            page.screenshot(path=str(Path(output_path) / "private-expanded.png"), full_page=True)
        _copy(page, _export_text(mode))
        _download(page, mode, _export_text(mode))
        page.wait_for_function("window.exportBlobUrls.size === 0")
    assert [request.post_data_json for request in inventory] == [{"mode": "sharing"}, {"mode": "private"}]
    assert page.evaluate("window.exportBlobTypes") == ["application/yaml", "application/yaml"]
    summary.focus()
    summary.press("Space")
    expect(output).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    summary.click()
    page.locator("#app-definition-scope").evaluate("element => element.scrollIntoView({block: 'start'})")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(Path(output_path) / "private-mobile.png"))


@pytest.mark.parametrize("token_hash", [None, TOKEN_HASH], ids=["empty", "populated"])
def test_private_token_records_remain_ready_with_exact_preview_copy_and_download(
    export_page: Page,
    stack: LocalStack,
    output_path: str,
    token_hash: str | None,
) -> None:
    page = export_page
    inventory = _fake_exports(page, token_hash=token_hash)
    _open(page, stack)
    sharing_text = _export_text("sharing", token_hash)
    _ready(page, sharing_text)
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    private_text = _export_text("private", token_hash)
    _ready(page, private_text)
    page.locator("#app-definition-preview > summary").click()
    output = page.get_by_role("region", name="App definitions YAML")
    expect(output).to_be_visible()
    records = yaml.safe_load(output.text_content())["platform_api_tokens"]
    assert len(records) == (1 if token_hash else 0)
    if records:
        assert records[0]["token_hash"] == token_hash
        assert records[0]["expires_at"] is None
    assert RAW_KEY not in private_text
    assert output.locator("img").count() == 0
    assert page.evaluate("window.exportXss === undefined")
    output.evaluate("element => element.scrollTop = element.scrollHeight")
    page.screenshot(path=str(Path(output_path) / "private-token-records.png"), full_page=True)
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
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
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
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _cleared(page)
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page)
    if latest_mode == "private":
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
    assert page.evaluate("window.exportSignals[1].aborted")
    assert len(pending) == (3 if latest_mode == "private" else 2)
    latest_text = _export_text(latest_mode, "b" * 64)
    _fulfill(pending[-1], latest_text)
    _ready(page, latest_text)
    # Neither a stale success nor a stale error may replace current text or feedback.
    if late_fails:
        _fulfill(pending[0], '{"error":"Export failed."}', status=500, content_type="application/json")
    else:
        _fulfill(pending[0], _export_text("private"))
    if latest_mode == "private":
        _fulfill(pending[1], _export_text("sharing"))
    page.wait_for_function("window.exportCompletions === window.exportSignals.length")
    _ready(page, latest_text)
    assert TOKEN_HASH not in page.locator("#app-definition-output").text_content()
    _copy(page, latest_text)
    _download(page, latest_mode, latest_text)


@pytest.mark.parametrize("latest_fails", [False, True])
@pytest.mark.parametrize("body_fails", [False, True])
@pytest.mark.parametrize("latest_mode", ["sharing", "private"])
def test_private_body_finishing_after_mode_change_cannot_replace_current_export_or_its_error(
    export_page: Page, stack: LocalStack, latest_fails: bool, body_fails: bool, latest_mode: str
) -> None:
    page = export_page
    # Return private headers immediately, then hold text() despite cancellation.
    page.add_init_script("""
      const realFetch = window.fetch;
      window.fetch = async (url, options) => {
        const response = await realFetch(url, options);
        if (url === '/api/app-definitions/export' && JSON.parse(options.body).mode === 'private'
            && !window.privateBodyHeld) {
          window.privateBodyHeld = true;
          const text = await response.text();
          response.text = () => new Promise((resolve, reject) => {
            window.releasePrivateBody = fails => fails ? reject(new Error('late body failure')) : resolve(text);
          });
        }
        return response;
      };
    """)
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    page.wait_for_function("typeof window.releasePrivateBody === 'function'")
    _cleared(page)
    if latest_fails:
        page.route(
            f"**{EXPORT_PATH}",
            lambda route: _fulfill(route, '{"error":"Export failed."}', status=500, content_type="application/json"),
        )
    else:
        _fake_exports(page, token_hash="b" * 64)
    page.get_by_role("radio", name="Sharing", exact=True).check()
    if latest_mode == "private":
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
    latest_text = _export_text(latest_mode, "b" * 64)
    if latest_fails:
        _cleared(page, ERROR)
    else:
        _ready(page, latest_text)
    page.evaluate("fails => window.releasePrivateBody(fails)", body_fails)
    if latest_fails:
        _cleared(page, ERROR)
    else:
        _ready(page, latest_text)
        _copy(page, latest_text)
        _download(page, latest_mode, latest_text)


@pytest.mark.parametrize(
    ("body", "http_status", "content_type"),
    [
        ('{"error":"Export failed."}', 500, "application/json"),
        ('{"error":"Log in."}', 401, "application/json"),
        ("<html><h1>Log in</h1></html>", 200, "text/html"),
        (_export_text("sharing"), 200, "application/json"),
        (_export_text("sharing"), 200, "text/plain"),
        (_export_text("sharing"), 200, "application/yaml-invalid"),
        (_export_text("sharing"), 200, ""),
        ("", 200, "application/yaml"),
        (" \t\r\n\n", 200, "application/yaml"),
        ("", 204, "application/yaml"),
    ],
    ids=[
        "http-error",
        "unauthenticated",
        "login-html",
        "json-mime",
        "plain-text-mime",
        "mime-prefix",
        "mime-empty",
        "empty",
        "whitespace",
        "no-content",
    ],
)
def test_invalid_response_leaves_no_previous_private_export_actionable(
    export_page: Page, stack: LocalStack, body: str, http_status: int, content_type: str
) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, _export_text("private"))
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, body, http_status, content_type))
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page, ERROR)


@pytest.mark.parametrize("mode", ["sharing", "private"])
@pytest.mark.parametrize(
    "headers",
    [
        {"X-App-Definitions-Mode": None},
        {"X-App-Definitions-Mode": ""},
        {"X-App-Definitions-Mode": "Sharing"},
        {"X-App-Definitions-Mode": "sharing, private"},
        {"X-App-Definitions-Schema-Version": None},
        {"X-App-Definitions-Schema-Version": ""},
        {"X-App-Definitions-Schema-Version": "1"},
        {"X-App-Definitions-Schema-Version": "3"},
        {"X-App-Definitions-Schema-Version": "2.0"},
        {"X-App-Definitions-Schema-Version": "02"},
    ],
    ids=[
        "mode-missing",
        "mode-empty",
        "mode-case",
        "mode-duplicate",
        "schema-missing",
        "schema-empty",
        "schema-old",
        "schema-unsupported",
        "schema-float",
        "schema-leading-zero",
    ],
)
def test_invalid_metadata_cannot_fall_back_to_parsing_a_valid_yaml_body(
    export_page: Page, stack: LocalStack, mode: str, headers: dict[str, str | None]
) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, _export_text("private"))
    if mode == "private":
        page.get_by_role("radio", name="Sharing", exact=True).check()
        _ready(page, _export_text("sharing"))
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, _export_text(mode), headers=headers))
    page.get_by_role("radio", name="Sharing" if mode == "sharing" else PRIVATE_LABEL, exact=True).check()
    _cleared(page, ERROR)


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_mode_header_must_match_request_even_when_yaml_body_matches(
    export_page: Page, stack: LocalStack, mode: str
) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    other_mode = "private" if mode == "sharing" else "sharing"
    if mode == "sharing":
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
        _ready(page, _export_text("private"))
    page.route(
        f"**{EXPORT_PATH}",
        lambda route: _fulfill(route, _export_text(mode), headers={"X-App-Definitions-Mode": other_mode}),
    )
    page.get_by_role("radio", name="Sharing" if mode == "sharing" else PRIVATE_LABEL, exact=True).check()
    _cleared(page, ERROR)


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_yaml_charset_is_accepted(export_page: Page, stack: LocalStack, mode: str) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    if mode == "sharing":
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
        _ready(page, _export_text("private"))
    text = _export_text(mode)
    page.route(
        f"**{EXPORT_PATH}",
        lambda route: _fulfill(
            route,
            text,
            content_type="Application/Yaml; charset=utf-8",
        ),
    )
    page.get_by_role("radio", name="Sharing" if mode == "sharing" else PRIVATE_LABEL, exact=True).check()
    _ready(page, text)


def test_yaml_looking_name_is_opaque_and_mode_binding_uses_headers(export_page: Page, stack: LocalStack) -> None:
    page = export_page
    # Valid YAML can spell keys differently and contain misleading metadata inside scalar text.
    text = (
        '# café: server formatting must survive\n---\n"schema_version": 2\n"mode": \'private\'\napps: []\n'
        "platform_api_tokens:\n    - name: |\n"
        "        mode: sharing\n        schema_version: 999\n        platform_api_tokens: []\n"
        f'      token_hash: "{TOKEN_HASH}"\n      expires_at: null\n...\n\n'
    )
    assert "mode: sharing\n" in yaml.safe_load(text)["platform_api_tokens"][0]["name"]
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, text, headers={"X-App-Definitions-Mode": "private"}))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, text)
    _copy(page, text)
    _download(page, "private", text)
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page, ERROR)


def test_successful_yaml_redirect_is_rejected(export_page: Page, stack: LocalStack) -> None:
    page = export_page
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, _export_text("private"))
    # Playwright routes only the first URL in a redirect chain. Preserve the POST and let
    # the target reach the real owner endpoint, so every check except redirected succeeds.
    target = f"{EXPORT_PATH}?redirected=1"
    page.route(f"**{EXPORT_PATH}", lambda route: route.fulfill(status=307, headers={"Location": target}))
    with page.expect_response(f"**{target}") as response_info:
        page.get_by_role("radio", name="Sharing", exact=True).check()
    assert response_info.value.ok
    assert response_info.value.request.redirected_from is not None
    assert response_info.value.headers["content-type"].split(";")[0] == "application/yaml"
    for name, value in _export_headers("sharing").items():
        assert response_info.value.headers[name.lower()] == value
    _cleared(page, ERROR)


@pytest.mark.parametrize("stage", ["fetch", "body"])
def test_async_failure_leaves_no_previous_private_export_actionable(
    export_page: Page, stack: LocalStack, stage: str
) -> None:
    page = export_page
    page.add_init_script("""
      const realFetch = window.fetch;
      window.fetch = async (url, options) => {
        if (url === '/api/app-definitions/export' && window.failExportStage === 'fetch') {
          throw new Error('synthetic network failure');
        }
        const response = await realFetch(url, options);
        if (url === '/api/app-definitions/export' && window.failExportStage === 'body') {
          response.text = () => Promise.reject(new Error('synthetic body failure'));
        }
        return response;
      };
    """)
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, _export_text("private"))
    page.evaluate("stage => window.failExportStage = stage", stage)
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _cleared(page, ERROR)


@pytest.mark.parametrize("outcome", ["resolve", "reject"])
@pytest.mark.parametrize("latest_mode", ["sharing", "private"])
def test_clipboard_completion_after_mode_change_cannot_fallback_or_change_feedback(
    export_page: Page, stack: LocalStack, outcome: str, latest_mode: str
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
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, _export_text("private"))
    page.get_by_role("button", name="Copy", exact=True).click()
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _ready(page, _export_text("sharing"))
    if latest_mode == "private":
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
        _ready(page, _export_text("private"))
    page.evaluate("outcome => window.finishCopy[outcome]()", outcome)
    assert page.evaluate("window.fallbackCopies") == []
    _ready(page, _export_text(latest_mode))


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
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
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
        expect(page.get_by_role("radio", name=PRIVATE_LABEL)).not_to_be_checked()
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
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
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
    empty_text = _export_text("private", token_hash=None)
    page.route(f"**{EXPORT_PATH}", lambda route: _fulfill(route, empty_text))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, empty_text)
    scan("private-empty-tokens")


def test_real_owner_endpoint_exports_stored_inventory(export_page: Page, stack: LocalStack, output_path: str) -> None:
    page = export_page
    # No export route mock: exercise the current checkout's authenticated owner endpoint.
    with page.expect_response(f"**{EXPORT_PATH}") as response_info:
        _open(page, stack)
    response = response_info.value
    assert response.ok
    assert response.request.headers["accept"] == "application/yaml"
    assert response.request.headers["content-type"] == "application/json"
    assert response.request.post_data_json == {"mode": "sharing"}
    assert response.request.redirected_from is None
    assert response.headers["content-type"].split(";")[0] == "application/yaml"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-app-definitions-mode"] == "sharing"
    assert response.headers["x-app-definitions-schema-version"] == "2"
    assert "x-app-definitions-missing-count" not in response.headers
    sharing = response.text()
    _ready(page, sharing)
    data = yaml.safe_load(sharing)
    assert data["schema_version"] == 2
    assert data["mode"] == "sharing"
    apps = data["apps"]
    assert len(apps) == 1
    assert apps[0]["name"] == "export-fixture"
    assert apps[0]["port_mappings"] == [{"label": "web", "container_port": 8080, "host_port": 29001}]
    assert set(apps[0]) == {"name", "source", "port_mappings"}
    assert "platform_api_tokens" not in data
    assert TOKEN_HASH not in sharing
    assert RAW_KEY not in sharing
    page.locator("#app-definition-preview > summary").click()
    page.screenshot(path=str(Path(output_path) / "real-sharing-yaml.png"), full_page=True)
    _copy(page, sharing)
    _download(page, "sharing", sharing)
    with page.expect_response(f"**{EXPORT_PATH}") as response_info:
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
    response = response_info.value
    assert response.ok
    assert response.request.headers["accept"] == "application/yaml"
    assert response.request.headers["content-type"] == "application/json"
    assert response.request.post_data_json == {"mode": "private"}
    assert response.request.redirected_from is None
    assert response.headers["content-type"].split(";")[0] == "application/yaml"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-app-definitions-mode"] == "private"
    assert response.headers["x-app-definitions-schema-version"] == "2"
    assert "x-app-definitions-missing-count" not in response.headers
    assert yaml.safe_load(response.text()) == {
        **data,
        "mode": "private",
        "platform_api_tokens": [{"name": "browser token", "token_hash": TOKEN_HASH, "expires_at": None}],
    }
    assert RAW_KEY not in response.text()
    _ready(page, response.text())
    page.screenshot(path=str(Path(output_path) / "real-private-yaml.png"), full_page=True)
    _copy(page, response.text())
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
        page.get_by_role("radio", name=PRIVATE_LABEL).check()
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        page.locator("#app-definition-preview > summary").click()
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
          window.bfcacheShows = [];
          window.addEventListener('pagehide', () => {
            window.exportClearedOnLeave = document.getElementById('app-definition-output').textContent === ''
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
        expect(page.get_by_role("radio", name=PRIVATE_LABEL)).not_to_be_checked()
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        assert yaml.safe_load(page.locator("#app-definition-output").text_content())["mode"] == "sharing"
        assert [json.loads(body) for body in inventory[count:]] == [{"mode": "sharing"}]
        assert page.locator("#app-definition-preview").get_attribute("open") is None
    finally:
        browser.close()
