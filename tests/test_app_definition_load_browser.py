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
IMPORT = "/api/app-definitions/import-secrets"
ADD = "/api/add_app"
SECRET = "SYNTHETIC-UPLOAD-PRIVATE-VALUE"
FILE = "# exported YAML\nschema_version: 1\nmode: private\napps: []\nsecret_values:\n  TOKEN: " + SECRET + "\n"
CONSENT = "Import 1 secret values (replaces existing values with the same names)"
READ_ERROR = "Could not read app definitions. Check the YAML file and your owner login, then choose the file again."
IMPORT_ERROR = (
    "Secret import failed. Some values may have been saved. No apps were deployed. Check Secrets before loading again."
)


@pytest.fixture
def load_page(export_page: Page) -> Page:
    return export_page


def _app(name: str = "first", status: str = "ready") -> dict:
    app = {
        "name": name,
        "source_label": "https://example.invalid/" + name + ".git@main",
        "status": status,
        "secret_keys": ["TOKEN", "MISSING"],
    }
    if status == "ready":
        app["install"] = {
            "repo_url": "https://example.invalid/" + name + ".git@main",
            "app_name": name,
            "port_overrides": {"web": 29001},
            "permissions_v2_grants": [
                {"service_url": "github.com/imbue-openhost/openhost/services/secrets", "grant": {"key": key}}
                for key in ("TOKEN", "MISSING")
            ],
        }
    return app


def _plan(mode: str = "sharing", *, values: bool = False, apps: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "mode": mode,
        "apps": [_app()] if apps is None else apps,
        "secret_keys": ["TOKEN"] if values else [],
        "missing_secret_keys": ["MISSING"],
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
    expect(page.locator("#app-definition-consent")).not_to_be_checked()
    expect(page.locator("#app-definition-consent-label")).to_be_hidden()
    expect(page.locator("#app-definition-apps")).to_be_empty()
    expect(page.locator("#app-definition-secret-keys")).to_be_empty()
    expect(page.locator("#app-definition-missing-keys")).to_be_empty()
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
    assert SECRET not in page.content()


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
        route.fulfill(json={"ok": True})
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
    _status(page, "Confirm secret replacement to load." if plan["secret_keys"] else "Ready to load.")
    return inventory


def test_upload_private_consent_import_before_exact_sequential_installs_and_skips(
    load_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = load_page
    plan = _plan(
        "private",
        values=True,
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
    plan["apps"][4]["secret_keys"] = ["OTHER"]
    plan["apps"][4]["install"]["permissions_v2_grants"] = [
        {"service_url": "github.com/imbue-openhost/openhost/services/secrets", "grant": {"key": "OTHER"}}
    ]
    mutations = _fake_mutations(page, hold=True)
    parses = _start(page, stack, plan)
    assert len(parses) == 1
    assert parses[0].method == "POST"
    assert parses[0].post_data_json == {"content": FILE}
    assert parses[0].headers["accept"] == "application/json"
    assert parses[0].headers["content-type"] == "application/json"
    expect(page.locator("#app-definition-file")).to_have_attribute("accept", ".yaml,.yml")
    expect(page.locator("#app-definition-secret-keys")).to_have_text("1 secret values in file: TOKEN")
    expect(page.locator("#app-definition-missing-keys")).to_contain_text("Missing secret references (1): MISSING")
    expect(page.locator("#app-definition-apps li").nth(0)).to_contain_text("2 secret keys: TOKEN, MISSING")
    expect(page.locator("#app-definition-apps li").nth(1)).to_contain_text("Skipped: already exists")
    expect(page.locator("#app-definition-apps li").nth(1).get_by_role("link")).to_have_attribute(
        "href", "/app_detail/existing"
    )
    for index in (2, 3):
        expect(page.locator("#app-definition-apps li").nth(index)).to_contain_text(
            "Skipped: source unavailable on this system"
        )
    button = page.get_by_role("button", name="Load apps", exact=True)
    checkbox = page.get_by_role("checkbox", name=CONSENT, exact=True)
    expect(checkbox).not_to_be_checked()
    expect(button).to_be_disabled()
    button.dispatch_event("click")
    assert mutations == []
    checkbox.focus()
    checkbox.press("Space")
    expect(button).to_be_enabled()
    checkbox.press("Space")
    expect(button).to_be_disabled()
    checkbox.press("Space")
    checkbox.press("Tab")
    expect(button).to_be_focused()
    button.press("Enter")
    _status(page, "Importing secret values…")
    expect(checkbox).to_be_disabled()
    expect(page.locator("#app-definition-file")).to_be_disabled()
    expect(button).to_be_disabled()
    _expect_mutations(page, mutations, 1)
    assert mutations[0].request.url.endswith(IMPORT)
    assert mutations[0].request.post_data_json == {"content": FILE, "replace_existing": True}
    button.dispatch_event("click")
    page.locator("#app-definition-file").dispatch_event("change")
    assert len(mutations) == 1
    _succeed(mutations[0])
    _status(page, "Requesting deployment for first…")
    _expect_mutations(page, mutations, 2)
    assert mutations[1].request.post_data_json == plan["apps"][0]["install"]
    _succeed(mutations[1])
    _status(page, "Requesting deployment for last…")
    _expect_mutations(page, mutations, 3)
    assert mutations[2].request.post_data_json == plan["apps"][4]["install"]
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
    assert SECRET not in page.content()
    assert "running" not in page.locator("#app-definition-load").inner_text().lower()
    page.screenshot(path=str(Path(output_path) / "loaded-apps-and-secrets.png"), full_page=True)


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_no_provided_values_needs_no_consent_or_secret_import(load_page: Page, stack: LocalStack, mode: str) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _start(page, stack, _plan(mode))
    expect(page.locator("#app-definition-consent-label")).to_be_hidden()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert [urlsplit(route.request.url).path for route in mutations] == [ADD]


@pytest.mark.parametrize("values", [False, True])
def test_secret_only_file_and_nothing_to_load(load_page: Page, stack: LocalStack, values: bool) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _fake_exports(page)
    _fake_plan(page, _plan("private", values=values, apps=[]))
    _open(page, stack)
    _upload(page, name="secrets.yml")
    if values:
        _status(page, "Confirm secret replacement to load.")
        page.get_by_role("checkbox", name=CONSENT).check()
        page.get_by_role("button", name="Load apps", exact=True).click()
        _status(page, "Load requests finished. Check the dashboard for deployment progress.")
        assert [urlsplit(route.request.url).path for route in mutations] == [IMPORT]
    else:
        _status(page, "Nothing to load.")
        expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
        assert not mutations


@pytest.mark.parametrize("failure", ["http", "401", "403", "network", "html", "invalid-json", "false", "redirect"])
def test_secret_import_failure_stops_all_apps_and_reports_possible_partial_save(
    load_page: Page, stack: LocalStack, failure: str
) -> None:
    page = load_page
    mutations = _fake_mutations(page, hold=True)
    _start(page, stack, _plan("private", values=True, apps=[_app(), _app("second")]))
    page.get_by_role("checkbox", name=CONSENT).check()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Importing secret values…")
    _expect_mutations(page, mutations, 1)
    route = mutations[0]
    if failure == "network":
        route.abort()
    elif failure == "html":
        route.fulfill(content_type="text/html", body="<h1>" + SECRET + "</h1>")
    elif failure == "invalid-json":
        route.fulfill(content_type="application/json", body="{")
    elif failure == "false":
        route.fulfill(json={"ok": False, "error": SECRET})
    elif failure == "redirect":
        route.fulfill(status=307, headers={"Location": "/dashboard"})
    else:
        route.fulfill(status=int(failure) if failure.isdigit() else 502, json={"error": SECRET, "partial": True})
    _status(page, IMPORT_ERROR)
    assert len(mutations) == 1
    expect(page.locator("#app-definition-apps")).to_contain_text("Not requested: load stopped.")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
    expect(page.locator("#app-definition-consent")).not_to_be_checked()
    assert SECRET not in page.content()


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
        route.fulfill(status=int(failure), json={"detail": SECRET})
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
    assert SECRET not in page.content()


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {**_plan(), "schema_version": 2},
        {**_plan(), "mode": "unknown"},
        {**_plan(), "secret_keys": ["TOKEN"]},
        {**_plan(), "secret_values": {"TOKEN": SECRET}},
        {**_plan(), "missing_secret_keys": "TOKEN"},
        {**_plan(), "apps": [None]},
        _plan(apps=[{**_app(), "status": "running"}]),
        _plan(apps=[{**_app(), "install": None}]),
        _plan(apps=[{**_app(), "name": 42}]),
        _plan(apps=[{**_app(), "secret_keys": [False]}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "grant_permissions_v2": True}}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "app_name": "different"}}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "port_overrides": {"web": "80"}}}]),
        _plan(apps=[{**_app(), "install": {**_app()["install"], "permissions_v2_grants": [None]}}]),
        _plan(apps=[_app(), _app()]),
    ],
    ids=[
        "null",
        "empty",
        "version",
        "mode",
        "sharing-values",
        "leaked-values",
        "missing-keys",
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
def test_invalid_plan_never_actionable_and_clears_previous_private_consent(
    load_page: Page, stack: LocalStack, invalid: dict | None
) -> None:
    page = load_page
    mutations = _fake_mutations(page)
    _start(page, stack, _plan("private", values=True))
    page.get_by_role("checkbox", name=CONSENT).check()
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
    _start(page, stack, _plan("private", values=True))
    page.get_by_role("checkbox", name=CONSENT).check()

    def respond(route: Route) -> None:
        if failure == "network":
            route.abort()
        elif failure == "html":
            route.fulfill(content_type="text/html", body=SECRET)
        elif failure == "invalid-json":
            route.fulfill(content_type="application/json", body=SECRET)
        elif failure == "redirect":
            route.fulfill(status=307, headers={"Location": "/dashboard"})
        else:
            route.fulfill(status=int(failure), json={"error": SECRET})

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
    parses = _start(page, stack, _plan("private", values=True))
    page.get_by_role("checkbox", name=CONSENT).check()
    _upload(page, "é" * (512 * 1024) + "a")
    _status(page, "Choose a YAML file no larger than 1 MiB.")
    _empty(page)
    assert len(parses) == 1
    assert page.evaluate("window.fileReads") == 1
    _upload(page, "#" * (1024 * 1024), "limit.yml")
    _status(page, "Confirm secret replacement to load.")
    assert len(parses) == 2
    page.locator("#app-definition-file").set_input_files([])
    _empty(page)
    _status(page, "")


@pytest.mark.parametrize(
    "invalid",
    [b"\xff", b"\xc3", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"],
    ids=["invalid-byte", "truncated", "overlong", "surrogate", "out-of-range"],
)
def test_invalid_utf8_value_is_rejected_before_post_and_clears_upload_buffers(
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
    parses = _start(page, stack, _plan("private", values=True))
    page.get_by_role("checkbox", name=CONSENT).check()
    payload = FILE.encode("utf-8").replace(SECRET.encode("utf-8"), b"prefix" + invalid + b"suffix")
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
    _fake_plan(page, _plan("private", values=True))
    _open(page, stack)
    page.evaluate("stage => window.holdStage = stage", stage)
    _upload(page)
    page.wait_for_function("window.releases.length === 1")
    _fake_plan(page, _plan(apps=[_app("current")]))
    if latest_fails:
        page.route(f"**{PARSE}", lambda route: route.fulfill(status=400, json={"error": SECRET}))
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
    expect(page.locator("#app-definition-consent-label")).to_be_hidden()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert len(mutations) == 1
    assert mutations[0].request.post_data_json == _app("current")["install"]
    assert SECRET not in page.content()


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
    _fake_plan(page, _plan("private", values=True, apps=[_app(), _app("second")]))
    _open(page, stack)
    page.evaluate("url => window.holdUrl = url", {"parse": PARSE, "import": IMPORT, "app": ADD}[stage])
    page.evaluate("value => window.holdFetch = value", held == "fetch")
    _upload(page)
    if stage != "parse":
        _status(page, "Confirm secret replacement to load.")
        page.get_by_role("checkbox", name=CONSENT).check()
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
    assert SECRET not in page.content()


@pytest.mark.parametrize("private", [False, True])
def test_actual_navigation_during_accepted_request_does_not_deploy_remaining_apps(
    load_page: Page, stack: LocalStack, private: bool
) -> None:
    page = load_page
    mutations = _fake_mutations(page, hold=True)
    _start(page, stack, _plan("private" if private else "sharing", values=private, apps=[_app(), _app("second")]))
    if private:
        page.get_by_role("checkbox", name=CONSENT).check()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Importing secret values…" if private else "Requesting deployment for first…")
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
    _fake_plan(page, _plan("private", values=True))
    _open(page, stack)
    for navigation in ("reload", "back", "return"):
        _upload(page)
        _status(page, "Confirm secret replacement to load.")
        page.get_by_role("checkbox", name=CONSENT).check()
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
    plan = _plan("private", values=True, apps=[_app(attack)])
    plan["apps"][0]["source_label"] = attack
    plan["apps"][0]["secret_keys"] = [attack]
    plan["secret_keys"] = [attack]
    plan["missing_secret_keys"] = [attack]
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
    expect(page.locator("#app-definition-secret-keys")).to_contain_text(json.dumps(attack))
    expect(page.locator("#app-definition-missing-keys")).to_contain_text(json.dumps(attack))
    scan("private-consent")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    scan("mobile")
    page.locator("#app-definition-consent").focus()
    page.keyboard.press("Space")
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    _status(page, "Importing secret values…")
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
    page.route(f"**{PARSE}", lambda route: route.fulfill(status=400, json={"error": SECRET}))
    _upload(page, "invalid")
    _status(page, READ_ERROR)
    scan("error")
    assert SECRET not in page.content()


def test_parent_export_preview_copy_download_and_private_resets_still_work_during_upload(
    load_page: Page, stack: LocalStack
) -> None:
    page = load_page
    parses = _start(page, stack, _plan("private", values=True))
    page.get_by_role("checkbox", name=CONSENT).check()
    _ready(page, _export_text("sharing"))
    page.locator("#app-definition-preview > summary").click()
    for mode in ("sharing", "private"):
        if mode == "private":
            page.get_by_role("radio", name="Private (includes secrets)").check()
        _ready(page, _export_text(mode))
        _copy(page, _export_text(mode))
        _download(page, mode, _export_text(mode))
        expect(page.get_by_role("checkbox", name=CONSENT)).to_be_checked()
        expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_enabled()
    assert len(parses) == 1
    assert SECRET not in page.content()
    page.reload()
    _empty(page)
    _ready(page, _export_text("sharing"))
    expect(page.get_by_role("radio", name="Sharing", exact=True)).to_be_checked()


def test_real_owner_parse_accepts_uploaded_export_without_values_in_plan(load_page: Page, stack: LocalStack) -> None:
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
    assert plan["schema_version"] == 1 and plan["mode"] == "sharing"
    assert plan["apps"][0]["name"] == "export-fixture"
    assert plan["apps"][0]["status"] == "existing"
    _status(page, "Nothing to load.")
    with page.expect_response("**/app_detail/export-fixture") as detail_info:
        page.locator("#app-definition-apps").get_by_role("link", name="App details").click()
    assert detail_info.value.ok
    assert page.url == f"{stack.router_url}/app_detail/export-fixture"
    _open(page, stack)
    private = yaml.safe_load(sharing)
    private.update(mode="private", secret_values={"TOKEN": SECRET}, missing_secret_keys=[])
    private["apps"][0]["name"] = "load-smoke"
    private["apps"][0]["secret_keys"] = ["TOKEN"]
    private["apps"][0]["port_mappings"][0]["host_port"] = 0
    with page.expect_response(f"**{PARSE}") as response_info:
        _upload(page, yaml.safe_dump(private))
    response = response_info.value
    assert response.ok
    assert response.json()["secret_keys"] == ["TOKEN"]
    assert SECRET not in response.text()
    assert "secret_values" not in response.json()
    _status(page, "Confirm secret replacement to load.")
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
    assert SECRET not in page.content()
    # Consume the real parser's exact grants and dynamic port with fake mutations only.
    mutations = _fake_mutations(page)
    page.get_by_role("checkbox", name=CONSENT).check()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert [urlsplit(route.request.url).path for route in mutations] == [IMPORT, ADD]
    assert mutations[1].request.post_data_json == response.json()["apps"][0]["install"]
    assert mutations[1].request.post_data_json["permissions_v2_grants"] == [
        {"service_url": "github.com/imbue-openhost/openhost/services/secrets", "grant": {"key": "TOKEN"}}
    ]


def test_export_download_upload_roundtrip_preserves_whitespace_and_control_secret_keys(
    load_page: Page, stack: LocalStack, output_path: str
) -> None:
    page = load_page
    names = [" ", "\t", "\x7f", "\x85", "\u00a0", "\u200b", "TOKEN"]
    displayed = ['" "', '"\\t"', '"\\u007f"', '"\\u0085"', '"\\u00a0"', '"\\u200b"', "TOKEN"]
    missing_names = ["\r"]
    document = {
        "schema_version": 1,
        "mode": "private",
        "apps": [
            {
                "name": "key-roundtrip",
                "source": {"kind": "remote", "repo_url": "https://example.invalid/keys.git", "ref": "main"},
                "port_mappings": [],
                "secret_keys": names + missing_names,
            }
        ],
        "secret_values": {key: SECRET + " café 🔐" for key in names},
        "missing_secret_keys": missing_names,
    }
    exported = dump_export_yaml(document)
    assert yaml.safe_load(exported) == document
    _fake_exports(page)
    _open(page, stack)
    _ready(page, _export_text("sharing"))
    page.route("**/api/app-definitions/export", lambda route: _fulfill(route, exported, missing_count=1))
    page.get_by_role("radio", name="Private (includes secrets)").check()
    _ready(page, exported, "Ready. 1 referenced secret is not configured.")
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
    assert response.json()["secret_keys"] == sorted(names)
    _status(page, "Confirm secret replacement to load.")
    expect(page.locator("#app-definition-apps .hint")).to_have_text(
        "8 secret keys: " + ", ".join(displayed + ['"\\r"'])
    )
    for spelling in displayed:
        expect(page.locator("#app-definition-secret-keys")).to_contain_text(spelling)
    expect(page.locator("#app-definition-missing-keys")).to_have_text(
        'Missing secret references (1): "\\r". Configure these before using the apps.'
    )
    checkbox = page.get_by_role(
        "checkbox", name="Import 7 secret values (replaces existing values with the same names)", exact=True
    )
    expect(checkbox).not_to_be_checked()
    expect(page.get_by_role("button", name="Load apps", exact=True)).to_be_disabled()
    assert SECRET not in page.content()
    page.screenshot(path=str(Path(output_path) / "quoted-secret-keys.png"), full_page=True)
    checkbox.check()
    page.get_by_role("button", name="Load apps", exact=True).click()
    _status(page, "Load requests finished. Check the dashboard for deployment progress.")
    assert [urlsplit(route.request.url).path for route in mutations] == [IMPORT, ADD]
    assert mutations[0].request.post_data_json == {"content": exported, "replace_existing": True}
    assert yaml.safe_load(mutations[0].request.post_data_json["content"]) == document
    assert mutations[1].request.post_data_json["permissions_v2_grants"] == [
        {"service_url": "github.com/imbue-openhost/openhost/services/secrets", "grant": {"key": key}}
        for key in names + missing_names
    ]
    assert SECRET not in page.content()


def test_actual_bfcache_return_clears_upload_and_late_restored_confirmation(
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
        # Routing disables BFCache. Use the real owner parser; no values are imported.
        _open(page, stack)
        expect(page.locator("#app-definition-status")).to_have_text("Ready.")
        data = yaml.safe_load(page.locator("#app-definition-output").text_content())
        data.update(mode="private", secret_values={"TOKEN": SECRET}, missing_secret_keys=[])
        _upload(page, yaml.safe_dump(data))
        _status(page, "Confirm secret replacement to load.")
        page.get_by_role("checkbox", name=CONSENT).check()
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
          window.cacheShows = [];
          window.addEventListener('pagehide', () => {
            window.loadCleared = document.getElementById('app-definition-file').value === ''
              && !document.getElementById('app-definition-consent').checked
              && document.getElementById('app-definition-deploy').disabled
              && document.getElementById('app-definition-apps').textContent === '';
          });
          window.addEventListener('pageshow', event => {
            window.cacheShows.push(event.persisted);
            document.getElementById('app-definition-consent').checked = true;
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
