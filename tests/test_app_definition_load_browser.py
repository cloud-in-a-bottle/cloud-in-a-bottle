import json
import time
from pathlib import Path
from urllib.parse import quote
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
from test_app_definition_export_browser import PRIVATE_LABEL
from test_app_definition_export_browser import _copy
from test_app_definition_export_browser import _download
from test_app_definition_export_browser import _export_text
from test_app_definition_export_browser import _fake_exports
from test_app_definition_export_browser import _fulfill
from test_app_definition_export_browser import _open
from test_app_definition_export_browser import _ready
from test_app_definition_export_browser import export_owner as export_owner
from test_app_definition_export_browser import export_page as export_page

from compute_space.tests.local_stack import LocalStack
from compute_space.web.helpers.app_definition_export import dump_export_yaml

PARSE = "/api/app-definitions/parse"
IMPORT = "/api/app-definitions/import-private"
ADD = "/api/add_app"
FAKE_HASH = "fa" * 32
FILE = (
    "# exported YAML\nschema_version: 2\nmode: private\napps: []\nplatform_api_tokens:\n"
    "  - name: fixture token\n    token_hash: " + FAKE_HASH + "\n    expires_at: null\n"
)
READ_ERROR = (
    "Could not read app definitions. Check the YAML file and your owner login, then choose the file again. "
    "Schema v1 files must be re-exported as schema v2."
)
IMPORT_ERROR = (
    "API-token import failed. Some records may already have been added. No apps were requested. "
    "Check API tokens in Settings before loading again; existing tokens are kept."
)


@pytest.fixture
def load_page(export_page: Page) -> Page:
    return export_page


def _app(name: str = "first", status: str = "ready") -> dict:
    app = {
        "name": name,
        "source_label": "https://example.invalid/" + name + ".git@main",
        "status": status,
    }
    if status == "ready":
        app["install"] = {
            "repo_url": "https://example.invalid/" + name + ".git@main",
            "app_name": name,
            "port_overrides": {"web": 29001},
        }
    return app


def _plan(mode: str = "sharing", *, tokens: bool = False, apps: list[dict] | None = None) -> dict:
    return {
        "schema_version": 2,
        "mode": mode,
        "apps": [_app()] if apps is None else apps,
        "platform_api_token_names": ["fixture token"] if tokens else [],
    }


def _upload(page: Page, content: str = FILE, name: str = "apps.yaml") -> None:
    page.locator("#app-definition-file").set_input_files(
        {"name": name, "mimeType": "application/yaml", "buffer": content.encode("utf-8")}
    )


def _status(page: Page, message: str) -> None:
    expect(page.locator("#app-definition-load-status")).to_have_text(message)


def _empty(page: Page) -> None:
    expect(page.locator("#app-definition-file")).to_have_value("")
    expect(page.locator("#app-definition-file")).to_be_enabled()
    expect(page.locator("#app-definition-load input[type=checkbox]")).to_have_count(0)
    expect(page.locator("#app-definition-apps")).to_be_empty()
    expect(page.locator("#app-definition-api-tokens")).to_be_empty()
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
    assert FAKE_HASH not in page.content()


def _fake_plan(page: Page, plan: dict) -> list[Request]:
    inventory: list[Request] = []

    def respond(route: Route) -> None:
        inventory.append(route.request)
        route.fulfill(json=plan)

    page.route(f"**{PARSE}", respond)
    return inventory


def _fake_mutations(page: Page, *, hold: bool = False) -> list[Route]:
    inventory: list[Route] = []

    def respond(route: Route) -> None:
        inventory.append(route)
        if not hold:
            _succeed(route)

    page.route(f"**{IMPORT}", respond)
    page.route(f"**{ADD}", respond)
    return inventory


def _succeed(route: Route) -> None:
    if route.request.url.endswith(IMPORT):
        route.fulfill(json={"ok": True, "added_api_token_count": 1, "existing_api_token_count": 0})
    else:
        route.fulfill(
            json={
                "ok": True,
                "app_id": "fakeid",
                "app_name": route.request.post_data_json["app_name"],
                "status": "building",
            }
        )


def _expect_mutations(page: Page, routes: list[Route], count: int) -> None:
    # UI feedback can precede Playwright dispatching the corresponding route callback.
    deadline = time.monotonic() + 5
    while len(routes) < count and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(routes) == count


def _start(page: Page, stack: LocalStack, plan: dict) -> list[Request]:
    _fake_exports(page)
    inventory = _fake_plan(page, plan)
    _open(page, stack)
    _empty(page)
    _upload(page)
    _status(page, "Ready to load.")
    return inventory


def test_upload_private_tokens_added_without_checkbox_before_exact_sequential_installs_and_skips(
    load_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = load_page
    plan = _plan(
        "private",
        tokens=True,
        apps=[
            _app(),
            _app("existing", "existing"),
            _app("local", "unavailable"),
            _app("unknown", "unavailable"),
            _app("last"),
        ],
    )
    plan["apps"][2]["source_label"] = "Local source"
    plan["apps"][3]["source_label"] = "Unknown source"
    mutations = _fake_mutations(page, hold=True)
    parses = _start(page, stack, plan)
    assert len(parses) == 1
    assert parses[0].method == "POST"
    assert parses[0].post_data_json == {"content": FILE}
    assert parses[0].headers["accept"] == "application/json"
    assert parses[0].headers["content-type"] == "application/json"
    expect(page.locator("#app-definition-file")).to_have_attribute("accept", ".yaml,.yml")
    expect(page.locator("#app-definition-api-tokens")).to_have_text(
        'Private file: 1 platform API-token records: "fixture token". '
        "Loading adds owner-access token records; existing tokens are kept."
    )
    expect(page.locator("#app-definition-file")).to_have_value("")
    expect(page.locator("#app-definition-apps li").nth(1)).to_contain_text("Skipped: already exists")
    expect(page.locator("#app-definition-apps li").nth(1).get_by_role("link")).to_have_attribute(
        "href", "/app_detail/existing"
    )
    for index in (2, 3):
        expect(page.locator("#app-definition-apps li").nth(index)).to_contain_text(
            "Skipped: source unavailable on this system"
        )
    button = page.get_by_role("button", name="Load apps", exact=True)
    expect(page.locator("#app-definition-load input[type=checkbox]")).to_have_count(0)
    expect(button).to_be_enabled()
    assert mutations == []
    page.locator("#app-definition-file").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#app-definition-apps li").nth(1).get_by_role("link")).to_be_focused()
    page.keyboard.press("Tab")
    expect(button).to_be_focused()
    button.press("Enter")
    _status(page, "Adding API-token records…")
    expect(page.locator("#app-definition-file")).to_be_disabled()
    expect(button).to_be_disabled()
    _expect_mutations(page, mutations, 1)
    assert mutations[0].request.url.endswith(IMPORT)
    assert mutations[0].request.post_data_json == {"content": FILE}
    button.dispatch_event("click")
    page.locator("#app-definition-file").dispatch_event("change")
    assert len(mutations) == 1
    _succeed(mutations[0])
    _status(page, "Requesting deployment for first…")
    _expect_mutations(page, mutations, 2)
    assert mutations[1].request.post_data_json == plan["apps"][0]["install"]
    assert set(mutations[1].request.post_data_json) == {"repo_url", "app_name", "port_overrides"}
    _succeed(mutations[1])
    _status(page, "Requesting deployment for last…")
    _expect_mutations(page, mutations, 3)
    assert mutations[2].request.post_data_json == plan["apps"][4]["install"]
    assert set(mutations[2].request.post_data_json) == {"repo_url", "app_name", "port_overrides"}
    _succeed(mutations[2])
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    for index, name in ((0, "first"), (4, "last")):
        row = page.locator("#app-definition-apps li").nth(index)
        expect(row).to_contain_text("Deployment started.")
        expect(row.get_by_role("link", name="App details")).to_have_attribute("href", "/app_detail/" + name)
    expect(page.get_by_role("link", name="Check dashboard progress")).to_have_attribute("href", "/dashboard")
    expect(page.locator("#app-definition-file")).to_be_enabled()
    expect(page.locator("#app-definition-file")).to_have_value("")
    expect(button).to_be_disabled()
    button.dispatch_event("click")
    assert len(mutations) == 3
    assert FAKE_HASH not in page.content()
    assert "running" not in page.locator("#app-definition-load").inner_text().lower()
    expect(page.locator("#app-definition-api-tokens")).to_contain_text("Added: 1. Already present: 0.")
    page.screenshot(path=str(Path(output_path) / "loaded-apps-and-api-tokens.png"), full_page=True)


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_no_token_records_skips_private_import(load_page: Page, stack: LocalStack, mode: str) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _start(page, stack, _plan(mode))
    expect(page.locator("#app-definition-load input[type=checkbox]")).to_have_count(0)
    if mode == "sharing":
        expect(page.locator("#app-definition-api-tokens")).to_be_hidden()
    else:
        expect(page.locator("#app-definition-api-tokens")).to_contain_text("0 platform API-token records.")
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert [urlsplit(route.request.url).path for route in mutations] == [ADD]


@pytest.mark.parametrize("tokens", [False, True])
def test_token_only_file_and_nothing_to_load(load_page: Page, stack: LocalStack, tokens: bool) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _fake_exports(page)
    _fake_plan(page, _plan("private", tokens=tokens, apps=[]))
    _open(page, stack)
    _upload(page, name="tokens.yml")
    expect(page.locator("#app-definition-load input[type=checkbox]")).to_have_count(0)
    if tokens:
        _status(page, "Ready to load.")
        page.get_by_role("button", name="Load apps", exact=True).click()
        _status(page, "Load requests finished. Check the dashboard for deployment progress.")
        assert [urlsplit(route.request.url).path for route in mutations] == [IMPORT]
        # Reupload is permitted: the backend owns additive import idempotence.
        page.route(
            f"**{IMPORT}",
            lambda route: route.fulfill(json={"ok": True, "added_api_token_count": 0, "existing_api_token_count": 1}),
        )
        _upload(page, name="tokens.yml")
        _status(page, "Ready to load.")
        with page.expect_request(f"**{IMPORT}") as request_info:
            page.get_by_role("button", name="Load apps", exact=True).click()
        _status(page, "Load requests finished. Check the dashboard for deployment progress.")
        assert request_info.value.post_data_json == {"content": FILE}
        expect(page.locator("#app-definition-api-tokens")).to_contain_text("Added: 0. Already present: 1.")
    else:
        _status(page, "Nothing to load.")
        expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
        assert not mutations


@pytest.mark.parametrize("failure", ["http", "401", "403", "network", "html", "invalid-json", "false", "redirect"])
def test_token_import_failure_stops_all_apps_and_reports_possible_additions(
    load_page: Page, stack: LocalStack, failure: str
) -> None:
    page = load_page
    mutations = _fake_mutations(page, hold=True)
    _start(page, stack, _plan("private", tokens=True, apps=[_app(), _app("second")]))
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Adding API-token records…")
    _expect_mutations(page, mutations, 1)
    route = mutations[0]
    if failure == "network":
        route.abort()
    elif failure == "html":
        route.fulfill(content_type="text/html", body="<h1>" + FAKE_HASH + "</h1>")
    elif failure == "invalid-json":
        route.fulfill(content_type="application/json", body="{")
    elif failure == "false":
        route.fulfill(json={"ok": False, "added_api_token_count": 0, "existing_api_token_count": 1})
    elif failure == "redirect":
        route.fulfill(status=307, headers={"Location": "/dashboard"})
    else:
        route.fulfill(status=int(failure) if failure.isdigit() else 502, json={"error": FAKE_HASH, "partial": True})
    _status(page, IMPORT_ERROR)
    assert len(mutations) == 1
    expect(page.locator("#app-definition-apps")).to_contain_text("Not requested: load stopped.")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
    assert FAKE_HASH not in page.content()


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"ok": True},
        {"ok": True, "added_api_token_count": 1},
        {"ok": True, "existing_api_token_count": 1},
        {"ok": "true", "added_api_token_count": 1, "existing_api_token_count": 0},
        {"ok": True, "added_api_token_count": True, "existing_api_token_count": 0},
        {"ok": True, "added_api_token_count": 0, "existing_api_token_count": "1"},
        {"ok": True, "added_api_token_count": -1, "existing_api_token_count": 0},
        {"ok": True, "added_api_token_count": 0, "existing_api_token_count": -1},
        {"ok": True, "added_api_token_count": 0.5, "existing_api_token_count": 0},
        {"ok": True, "added_api_token_count": 0, "existing_api_token_count": 0.5},
        {"ok": True, "added_api_token_count": 2**53, "existing_api_token_count": 0},
        {"ok": True, "added_api_token_count": 1, "existing_api_token_count": 0, "token_hash": FAKE_HASH},
    ],
)
def test_invalid_token_import_counts_stop_before_apps(load_page: Page, stack: LocalStack, result: dict | None) -> None:
    page = load_page
    mutations = _fake_mutations(page, hold=True)
    _start(page, stack, _plan("private", tokens=True))
    page.get_by_role("button", name="Load apps", exact=True).click()
    _expect_mutations(page, mutations, 1)
    mutations[0].fulfill(content_type="application/json", body=json.dumps(result))
    _status(page, IMPORT_ERROR)
    expect(page.locator("#app-definition-apps")).to_contain_text("Not requested: load stopped.")
    assert len(mutations) == 1
    assert FAKE_HASH not in page.content()


@pytest.mark.parametrize(
    "failure", ["400", "409", "500", "504", "github", "401", "403", "network", "html", "bad-success"]
)
def test_app_errors_continue_but_auth_loss_or_uncertain_results_stop(
    load_page: Page, stack: LocalStack, failure: str
) -> None:
    page = load_page
    mutations = _fake_mutations(page, hold=True)
    plan = _plan(apps=[_app(), _app("second")])
    _start(page, stack, plan)
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Requesting deployment for first…")
    _expect_mutations(page, mutations, 1)
    route = mutations[0]
    if failure == "github":
        route.fulfill(
            status=401,
            json={
                "detail": "GitHub authorization required",
                "extra": {"authorize_url": "javascript:window.loadXss=true"},
            },
        )
    elif failure == "network":
        route.abort()
    elif failure == "html":
        route.fulfill(content_type="text/html", body="<h1>Sign in</h1>")
    elif failure == "bad-success":
        route.fulfill(json={"ok": False})
    else:
        route.fulfill(status=int(failure), json={"detail": FAKE_HASH})
    if failure in ("400", "409", "github"):
        _status(page, "Requesting deployment for second…")
        _expect_mutations(page, mutations, 2)
        _succeed(mutations[1])
        _status(page, "Load requests finished. Check the dashboard for deployment progress.")
        expect(page.locator("#app-definition-apps li").nth(1)).to_contain_text("Deployment started.")
        if failure == "github":
            expect(page.get_by_role("link", name="Authorize in Add app")).to_have_attribute(
                "href", "/add_app?repo=" + quote(plan["apps"][0]["install"]["repo_url"], safe="")
            )
            assert page.evaluate("window.loadXss === undefined")
        else:
            expect(page.locator("#app-definition-apps li").nth(0)).to_contain_text(
                "Deployment failed (HTTP " + failure
            )
    else:
        expect(page.locator("#app-definition-load-status")).to_contain_text("Remaining apps were not requested.")
        expect(page.locator("#app-definition-apps li").nth(1)).to_contain_text("Not requested: load stopped.")
        if failure not in ("401", "403"):
            expect(page.locator("#app-definition-apps li").nth(0)).to_contain_text(
                "Deployment result uncertain. Check the dashboard before trying again."
            )
            _status(
                page,
                "Deployment result uncertain. Remaining apps were not requested. Check the dashboard before trying again.",
            )
            expect(page.get_by_role("link", name="Check dashboard progress")).to_have_attribute("href", "/dashboard")
        button = page.get_by_role("button", name="Load apps", exact=True)
        expect(button).to_be_disabled()
        expect(page.locator("#app-definition-file")).to_have_value("")
        button.dispatch_event("click")
        assert len(mutations) == 1
    assert page.url == f"{stack.router_url}/system/"
    assert FAKE_HASH not in page.content()


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {**_plan(), "schema_version": 1},
        {**_plan(), "mode": "unknown"},
        {**_plan(), "platform_api_token_names": ["fixture"]},
        {**_plan("private"), "platform_api_token_names": [None]},
        {**_plan(), "platform_api_token_names": "fixture"},
        {**_plan(), "platform_api_tokens": [{"name": "fixture", "token_hash": FAKE_HASH, "expires_at": None}]},
        {**_plan(), "secret_keys": []},
        {**_plan(), "missing_secret_keys": []},
        {**_plan(), "apps": [None]},
        _plan(apps=[{**_app(), "status": "running"}]),
        _plan(apps=[{**_app(), "install": None}]),
        _plan(apps=[{**_app(), "name": 42}]),
        _plan(apps=[{**_app(), "secret_keys": []}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "grant_permissions_v2": True}}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "app_name": "different"}}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "port_overrides": {"web": "80"}}}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "permissions_v2_grants": []}}]),
        _plan(apps=[_app(), _app()]),
    ],
    ids=[
        "null",
        "empty",
        "version",
        "mode",
        "sharing-tokens",
        "token-name-type",
        "token-names-type",
        "leaked-records",
        "obsolete-secret-keys",
        "obsolete-missing-keys",
        "app-null",
        "app-status",
        "no-install",
        "app-name",
        "secret-key",
        "all-permissions",
        "retargeted-name",
        "port",
        "grant",
        "duplicate-app",
    ],
)
def test_invalid_plan_never_actionable_and_clears_previous_private_file(
    load_page: Page, stack: LocalStack, invalid: dict | None
) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _start(page, stack, _plan("private", tokens=True))
    page.route(f"**{PARSE}", lambda route: route.fulfill(content_type="application/json", body=json.dumps(invalid)))
    _upload(page, "invalid YAML")
    _status(page, READ_ERROR)
    _empty(page)
    assert not mutations


@pytest.mark.parametrize("failure", ["400", "401", "403", "500", "network", "html", "invalid-json", "redirect"])
def test_parse_failures_are_safe_and_do_not_reuse_private_plan(
    load_page: Page, stack: LocalStack, failure: str
) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _start(page, stack, _plan("private", tokens=True))

    def respond(route: Route) -> None:
        if failure == "network":
            route.abort()
        elif failure == "html":
            route.fulfill(content_type="text/html", body=FAKE_HASH)
        elif failure == "invalid-json":
            route.fulfill(content_type="application/json", body=FAKE_HASH)
        elif failure == "redirect":
            route.fulfill(status=307, headers={"Location": "/dashboard"})
        else:
            route.fulfill(status=int(failure), json={"error": FAKE_HASH})

    page.route(f"**{PARSE}", respond)
    _upload(page, "bad: [")
    _status(page, READ_ERROR)
    _empty(page)
    assert not mutations


def test_byte_limit_before_file_read_and_clearing_selection(load_page: Page, stack: LocalStack) -> None:
    page = load_page
    page.add_init_script("""
      window.fileReads = 0;
      const read = File.prototype.arrayBuffer;
      File.prototype.arrayBuffer = function() { window.fileReads++; return read.call(this); };
    """)
    parses = _start(page, stack, _plan("private", tokens=True))
    _upload(page, "é" * (512 * 1024) + "a")
    _status(page, "Choose a YAML file no larger than 1 MiB.")
    _empty(page)
    assert len(parses) == 1
    assert page.evaluate("window.fileReads") == 1
    _upload(page, "#" * (1024 * 1024), "limit.yml")
    _status(page, "Ready to load.")
    assert len(parses) == 2
    page.locator("#app-definition-file").set_input_files([])
    _empty(page)
    _status(page, "")


@pytest.mark.parametrize(
    "invalid",
    [b"\xff", b"\xc3", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"],
    ids=["invalid-byte", "truncated", "overlong", "surrogate", "out-of-range"],
)
def test_invalid_utf8_is_rejected_before_post_and_clears_upload_buffers(
    load_page: Page, stack: LocalStack, invalid: bytes
) -> None:
    page = load_page
    page.add_init_script("""
      window.uploadBuffers = [];
      const read = File.prototype.arrayBuffer;
      File.prototype.arrayBuffer = async function() {
        const buffer = await read.call(this);
        window.uploadBuffers.push(new Uint8Array(buffer));
        return buffer;
      };
      File.prototype.text = () => { throw new Error('Lossy file decoding must not be used'); };
    """)
    mutations = _fake_mutations(page)
    parses = _start(page, stack, _plan("private", tokens=True))
    payload = FILE.encode("utf-8").replace(FAKE_HASH.encode("utf-8"), b"prefix" + invalid + b"suffix")
    page.locator("#app-definition-file").set_input_files(
        {"name": "invalid-utf8.yaml", "mimeType": "application/yaml", "buffer": payload}
    )
    _status(page, READ_ERROR)
    _empty(page)
    assert len(parses) == 1
    assert not mutations
    assert page.evaluate(
        "window.uploadBuffers.length === 2 && window.uploadBuffers.every(bytes => bytes.every(b => b === 0))"
    )


@pytest.mark.parametrize("stage", ["fetch", "body", "file"])
@pytest.mark.parametrize("late_fails", [False, True])
@pytest.mark.parametrize("latest_fails", [False, True])
def test_changing_upload_ignores_old_file_fetch_and_json_completions(
    load_page: Page, stack: LocalStack, stage: str, late_fails: bool, latest_fails: bool
) -> None:
    page = load_page
    page.add_init_script("""
      window.loadSignals = [];
      window.releases = [];
      window.completed = 0;
      const realFetch = window.fetch;
      const hold = value => new Promise((resolve, reject) => window.releases.push(fails => {
        window.completed++; fails ? reject(new Error('late failure')) : resolve(value);
      }));
      window.fetch = async (url, options) => {
        if (url !== '/api/app-definitions/parse') return realFetch(url, options);
        window.loadSignals.push(options.signal);
        const response = await realFetch(url, {...options, signal: undefined});
        if (window.holdStage === 'fetch') { window.holdStage = ''; return hold(response); }
        if (window.holdStage === 'body') {
          window.holdStage = '';
          const body = await response.json(); response.json = () => hold(body);
        }
        return response;
      };
      const read = File.prototype.arrayBuffer;
      File.prototype.arrayBuffer = async function() {
        const buffer = await read.call(this);
        if (window.holdStage === 'file') {
          window.holdStage = '';
          window.heldFileBytes = new Uint8Array(buffer);
          return hold(buffer);
        }
        return buffer;
      };
    """)
    mutations = _fake_mutations(page)
    _fake_exports(page)
    _fake_plan(page, _plan("private", tokens=True))
    _open(page, stack)
    page.evaluate("stage => window.holdStage = stage", stage)
    _upload(page)
    page.wait_for_function("window.releases.length === 1")
    _fake_plan(page, _plan(apps=[_app("current")]))
    if latest_fails:
        page.route(f"**{PARSE}", lambda route: route.fulfill(status=400, json={"error": FAKE_HASH}))
    # The same filename can contain different content; identity must be tied to the selection generation.
    _upload(page, "new file")
    _status(page, READ_ERROR if latest_fails else "Ready to load.")
    if stage != "file":
        assert page.evaluate("window.loadSignals[0].aborted")
    page.evaluate("fails => window.releases[0](fails)", late_fails)
    page.wait_for_function("window.completed === 1")
    if stage == "file" and not late_fails:
        assert page.evaluate("window.heldFileBytes.every(byte => byte === 0)")
    if latest_fails:
        _status(page, READ_ERROR)
        _empty(page)
        page.get_by_role("button", name="Load apps", exact=True).dispatch_event("click")
        assert not mutations
        return
    _status(page, "Ready to load.")
    expect(page.locator("#app-definition-apps")).to_contain_text("current")
    expect(page.locator("#app-definition-api-tokens")).to_be_hidden()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert len(mutations) == 1
    assert mutations[0].request.post_data_json == _app("current")["install"]
    assert FAKE_HASH not in page.content()


@pytest.mark.parametrize("stage", ["parse", "import", "app"])
@pytest.mark.parametrize("held", ["fetch", "body"])
@pytest.mark.parametrize("late_fails", [False, True])
def test_pagehide_aborts_inflight_work_resets_private_state_and_never_queues_more_apps(
    load_page: Page, stack: LocalStack, stage: str, held: str, late_fails: bool
) -> None:
    page = load_page
    page.add_init_script("""
      const realFetch = window.fetch;
      window.heldSignals = [];
      window.fetch = async (url, options) => {
        if (url !== window.holdUrl) return realFetch(url, options);
        window.heldSignals.push(options.signal);
        const response = await realFetch(url, {...options, signal: undefined});
        const data = await response.json();
        const hold = value => new Promise((resolve, reject) => {
          window.finishHeld = fails => fails ? reject(new Error('late failure')) : resolve(value);
        });
        if (window.holdFetch) { response.json = async () => data; return hold(response); }
        response.json = () => hold(data);
        return response;
      };
    """)
    mutations = _fake_mutations(page)
    _fake_exports(page)
    _fake_plan(page, _plan("private", tokens=True, apps=[_app(), _app("second")]))
    _open(page, stack)
    page.evaluate("url => window.holdUrl = url", {"parse": PARSE, "import": IMPORT, "app": ADD}[stage])
    page.evaluate("value => window.holdFetch = value", held == "fetch")
    _upload(page)
    if stage != "parse":
        _status(page, "Ready to load.")
        page.get_by_role("button", name="Load apps", exact=True).click()
    page.wait_for_function("typeof window.finishHeld === 'function'")
    count = len(mutations)
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    _empty(page)
    assert page.evaluate("window.heldSignals[0].aborted")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    # A new selection may not receive either old feedback or the old install payload.
    _fake_plan(page, _plan(apps=[_app("new")]))
    page.evaluate("window.holdUrl = ''")
    _upload(page, "current")
    _status(page, "Ready to load.")
    page.evaluate("fails => window.finishHeld(fails)", late_fails)
    _status(page, "Ready to load.")
    expect(page.locator("#app-definition-apps")).to_contain_text("new")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_enabled()
    assert len(mutations) == count
    assert FAKE_HASH not in page.content()


@pytest.mark.parametrize("private", [False, True])
def test_actual_navigation_during_accepted_request_does_not_deploy_remaining_apps(
    load_page: Page, stack: LocalStack, private: bool
) -> None:
    page = load_page
    mutations = _fake_mutations(page, hold=True)
    _start(page, stack, _plan("private" if private else "sharing", tokens=private, apps=[_app(), _app("second")]))
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Adding API-token records…" if private else "Requesting deployment for first…")
    _expect_mutations(page, mutations, 1)
    page.goto(f"{stack.router_url}/dashboard")
    # The server can finish an accepted request even after the browser aborts its response.
    _succeed(mutations[0])
    page.go_back()
    _empty(page)
    _status(page, "")
    assert len(mutations) == 1


def test_navigation_back_reload_and_return_start_with_empty_upload(load_page: Page, stack: LocalStack) -> None:
    page = load_page
    _fake_exports(page)
    _fake_plan(page, _plan("private", tokens=True))
    _open(page, stack)
    for navigation in ("reload", "back", "return"):
        _upload(page)
        _status(page, "Ready to load.")
        if navigation == "reload":
            page.reload()
        else:
            page.goto(f"{stack.router_url}/dashboard")
            if navigation == "back":
                page.go_back()
            else:
                _open(page, stack)
        _empty(page)


def test_text_only_untrusted_plan_mobile_keyboard_and_wcag_states(
    load_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = load_page
    attack = 'café <img src=x onerror="window.loadXss=true">/' + "x" * 180
    plan = _plan("private", tokens=True, apps=[_app(attack)])
    plan["apps"][0]["source_label"] = attack
    plan["platform_api_token_names"] = [attack]
    mutations = _fake_mutations(page, hold=True)
    _start(page, stack, plan)
    axe = Axe()

    def scan(state: str) -> None:
        results = axe.run(page, options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}})
        assert not results.response["violations"], f"{state}: {results.response['violations']}"
        page.screenshot(path=str(Path(output_path) / f"loader-{state}.png"), full_page=True)

    assert page.locator("#app-definition-load img").count() == 0
    assert page.evaluate("window.loadXss === undefined")
    expect(page.locator("#app-definition-apps")).to_contain_text(attack)
    expect(page.locator("#app-definition-api-tokens")).to_contain_text(json.dumps(attack))
    scan("private-tokens")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    scan("mobile")
    page.locator("#app-definition-file").focus()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    _status(page, "Adding API-token records…")
    scan("importing")
    _expect_mutations(page, mutations, 1)
    _succeed(mutations[0])
    _status(page, "Requesting deployment for " + attack + "…")
    scan("deploying")
    _expect_mutations(page, mutations, 2)
    _succeed(mutations[1])
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    expect(page.locator("#app-definition-apps").get_by_role("link", name="App details")).to_have_attribute(
        "href", "/app_detail/" + quote(attack, safe="~()*!.'")
    )
    scan("started")
    page.route(f"**{PARSE}", lambda route: route.fulfill(status=400, json={"error": FAKE_HASH}))
    _upload(page, "invalid")
    _status(page, READ_ERROR)
    scan("error")
    assert FAKE_HASH not in page.content()


def test_parent_export_preview_copy_download_and_private_resets_still_work_during_upload(
    load_page: Page, stack: LocalStack
) -> None:
    page = load_page
    parses = _start(page, stack, _plan("private", tokens=True))
    _ready(page, _export_text("sharing"))
    page.locator("#app-definition-preview > summary").click()
    for mode in ("sharing", "private"):
        if mode == "private":
            page.get_by_role("radio", name=PRIVATE_LABEL).check()
        _ready(page, _export_text(mode))
        _copy(page, _export_text(mode))
        _download(page, mode, _export_text(mode))
        expect(page.locator("#app-definition-load input[type=checkbox]")).to_have_count(0)
        expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_enabled()
    assert len(parses) == 1
    assert FAKE_HASH not in page.content()
    page.reload()
    _empty(page)
    _ready(page, _export_text("sharing"))
    expect(page.get_by_role("radio", name="Sharing", exact=True)).to_be_checked()


def test_real_owner_parse_accepts_uploaded_export_without_records_in_plan(load_page: Page, stack: LocalStack) -> None:
    page = load_page
    _open(page, stack)
    _ready_status = page.locator("#app-definition-status")
    expect(_ready_status).to_have_text("Ready.")
    sharing = page.locator("#app-definition-output").text_content()
    assert sharing is not None
    with page.expect_response(f"**{PARSE}") as response_info:
        _upload(page, sharing)
    response = response_info.value
    assert response.ok
    assert response.headers["cache-control"] == "no-store"
    plan = response.json()
    assert plan["schema_version"] == 2 and plan["mode"] == "sharing"
    assert plan["platform_api_token_names"] == []
    assert plan["apps"][0]["name"] == "export-fixture"
    assert plan["apps"][0]["status"] == "existing"
    _status(page, "Nothing to load.")
    with page.expect_response("**/app_detail/export-fixture") as detail_info:
        page.locator("#app-definition-apps").get_by_role("link", name="App details").click()
    assert detail_info.value.ok
    assert page.url == f"{stack.router_url}/app_detail/export-fixture"
    _open(page, stack)
    private = yaml.safe_load(sharing)
    private.update(mode="private", platform_api_tokens=yaml.safe_load(FILE)["platform_api_tokens"])
    private["apps"][0]["name"] = "load-smoke"
    private["apps"][0]["port_mappings"][0]["host_port"] = 0
    with page.expect_response(f"**{PARSE}") as response_info:
        _upload(page, yaml.safe_dump(private))
    response = response_info.value
    assert response.ok
    assert response.json()["platform_api_token_names"] == ["fixture token"]
    assert set(response.json()) == {"schema_version", "mode", "apps", "platform_api_token_names"}
    assert FAKE_HASH not in response.text()
    _status(page, "Ready to load.")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_enabled()
    assert FAKE_HASH not in page.content()
    # Consume the real parser's dynamic port with fake mutations only.
    mutations = _fake_mutations(page)
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert [urlsplit(route.request.url).path for route in mutations] == [IMPORT, ADD]
    assert mutations[1].request.post_data_json == response.json()["apps"][0]["install"]
    assert set(mutations[1].request.post_data_json) == {"repo_url", "app_name", "port_overrides"}
    assert mutations[1].request.post_data_json["port_overrides"] == {"web": 0}


def test_export_download_upload_roundtrip_preserves_empty_duplicate_and_odd_token_names(
    load_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = load_page
    names = ["", " ", "\t", "\x7f", "\x85", "\u00a0", "\u200b", "café", "\r", "same", "same"]
    displayed = [
        '""',
        '" "',
        '"\\t"',
        '"\\u007f"',
        '"\\u0085"',
        '"\\u00a0"',
        '"\\u200b"',
        "café",
        '"\\r"',
        "same",
        "same",
    ]
    document = {
        "schema_version": 2,
        "mode": "private",
        "apps": [
            {
                "name": "token-roundtrip",
                "source": {"kind": "remote", "repo_url": "https://example.invalid/app.git", "ref": "main"},
                "port_mappings": [],
            }
        ],
        "platform_api_tokens": [
            {"name": name, "token_hash": f"{index:064x}", "expires_at": None} for index, name in enumerate(names)
        ],
    }
    exported = dump_export_yaml(document)
    assert yaml.safe_load(exported) == document
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.route("**/api/app-definitions/export", lambda route: _fulfill(route, exported))
    page.get_by_role("radio", name=PRIVATE_LABEL).check()
    _ready(page, exported)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download", exact=True).click()
    downloaded = download_info.value.path()
    assert Path(downloaded).read_bytes() == exported.encode("utf-8")
    _fake_exports(page)
    page.get_by_role("radio", name="Sharing", exact=True).check()
    _ready(page, _export_text("sharing"))

    # Only mutations are faked. The owner parser validates the actual downloaded YAML file.
    mutations = _fake_mutations(page)
    with page.expect_response(f"**{PARSE}") as response_info:
        page.locator("#app-definition-file").set_input_files(downloaded)
    response = response_info.value
    assert response.ok
    assert response.request.post_data_json == {"content": exported}
    assert response.json()["platform_api_token_names"] == names
    assert all(record["token_hash"] not in response.text() for record in document["platform_api_tokens"])
    _status(page, "Ready to load.")
    expect(page.locator("#app-definition-api-tokens")).to_have_text(
        "Private file: 11 platform API-token records: "
        + ", ".join(displayed)
        + ". Loading adds owner-access token records; existing tokens are kept."
    )
    expect(page.locator("#app-definition-load input[type=checkbox]")).to_have_count(0)
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_enabled()
    assert all(record["token_hash"] not in page.content() for record in document["platform_api_tokens"])
    page.screenshot(path=str(Path(output_path) / "quoted-api-token-names.png"), full_page=True)
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert [urlsplit(route.request.url).path for route in mutations] == [IMPORT, ADD]
    assert mutations[0].request.post_data_json == {"content": exported}
    assert yaml.safe_load(mutations[0].request.post_data_json["content"]) == document
    assert set(mutations[1].request.post_data_json) == {"repo_url", "app_name", "port_overrides"}
    assert all(record["token_hash"] not in page.content() for record in document["platform_api_tokens"])


def test_real_parser_rejects_v1_with_reexport_guidance(load_page: Page, stack: LocalStack) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _open(page, stack)
    with page.expect_response(f"**{PARSE}") as response_info:
        _upload(page, "schema_version: 1\nmode: sharing\napps: []\n")
    assert response_info.value.status == 400
    _status(page, READ_ERROR)
    _empty(page)
    assert not mutations


@pytest.mark.parametrize("status", ["ready", "existing", "unavailable"])
def test_secrets_app_is_an_ordinary_app(load_page: Page, stack: LocalStack, status: str) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _fake_exports(page)
    plan = _plan(apps=[_app("Secrets", status), _app("other")])
    _fake_plan(page, plan)
    _open(page, stack)
    _upload(page)
    _status(page, "Ready to load.")
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    expected = [app["install"] for app in plan["apps"] if app["status"] == "ready"]
    assert [route.request.post_data_json for route in mutations] == expected
    assert all(urlsplit(route.request.url).path == ADD for route in mutations)


def test_actual_bfcache_return_clears_upload_and_late_restored_file(
    playwright: Playwright, stack: LocalStack, export_owner: requests.Session
) -> None:
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
        page = context.new_page()
        # Routing disables BFCache. Use the real owner parser; no records are imported.
        _open(page, stack)
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        data = yaml.safe_load(page.locator("#app-definition-output").text_content())
        data.update(mode="private", platform_api_tokens=yaml.safe_load(FILE)["platform_api_tokens"])
        _upload(page, yaml.safe_dump(data))
        _status(page, "Ready to load.")
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
          window.cacheShows = [];
          window.addEventListener('pagehide', () => {
            window.loadCleared = document.getElementById('app-definition-file').value === ''
              && document.getElementById('app-definition-api-tokens').textContent === ''
              && document.getElementById('app-definition-deploy').disabled
              && document.getElementById('app-definition-apps').textContent === '';
          });
          window.addEventListener('pageshow', event => {
            window.cacheShows.push(event.persisted);
            const restored = new DataTransfer();
            restored.items.add(new File(['restored'], 'private.yaml'));
            document.getElementById('app-definition-file').files = restored.files;
          });
        }""")
        page.goto(f"{stack.router_url}/dashboard")
        page.go_back(wait_until="commit")
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        assert page.evaluate("window.cacheShows") == [True]
        assert page.evaluate("window.loadCleared")
        _empty(page)
    finally:
        browser.close()
