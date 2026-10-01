import json
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import expect
from test_accessibility import WCAG_AA_TAGS

from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.test_managed_storage import ALLOCATION
from compute_space.tests.test_managed_storage import BINDING
from compute_space.tests.test_managed_storage import snapshot
from compute_space.tests.utils import managed_router


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = make_local_stack_config(
        str(tmp_path_factory.mktemp("managed-storage-ui")), port, "storage-ui", default_apps=[]
    )
    # This browser fixture renders stored state; it never mounts storage or starts apps.
    with sqlite3.connect(config.db_path) as db:
        db.execute("UPDATE archive_backend SET backend='disabled'")
    with managed_router(config):
        local = LocalStack(config)
        with complete_setup(local) as owner:
            yield local, owner


@pytest.fixture
def ui(page, stack):
    local, owner = stack
    page.context.add_cookies(
        [{"name": cookie.name, "value": cookie.value, "url": local.router_url} for cookie in owner.cookies]
    )

    def local_only(route):
        host = urlsplit(route.request.url).hostname or ""
        if host == "127.0.0.1" or host == "localhost" or host.endswith(".localhost"):
            route.continue_()
        else:
            route.abort()

    page.route("**/*", local_only)
    page.route("**/api/settings/update", lambda route: route.fulfill(json={"state": "UP_TO_DATE", "error": None}))
    page.route("**/ssh-status", lambda route: route.fulfill(json={"enabled": False}))
    state = {
        "backend": "s3",
        "s3_bucket": "managed-fixture",
        "s3_region": "auto",
        "s3_endpoint": "https://example.invalid",
        "managed_storage_allocation_id": ALLOCATION,
    }
    response = {"body": {"managed": True, "status": snapshot(), "error": None}, "status": 200, "calls": []}
    page.route("**/api/storage/archive_backend", lambda route: route.fulfill(json=state))

    def usage(route):
        response["calls"].append(route.request)
        route.fulfill(status=response["status"], json=response["body"])

    page.route("**/api/storage/managed_usage", usage)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page, local, state, response
    assert not errors, errors


def open_ui(ui):
    page, local, _, _ = ui
    result = page.goto(local.router_url + "/settings")
    assert result.ok
    return page.locator("#managed-storage-usage")


def screenshot(page, output_path, name):
    path = Path(output_path) / (name + ".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    page.locator("#managed-storage-usage").screenshot(path=str(path))


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_normal_usage_keyboard_and_accessibility(ui, width, output_path):
    page, _, _, response = ui
    page.set_viewport_size({"width": width, "height": 1000})
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    expect(region.get_by_role("progressbar", name="Stored capacity")).to_have_attribute("aria-valuenow", "25")
    expect(region.get_by_role("progressbar", name="Monthly activity allowance")).to_have_attribute(
        "aria-valuenow", "25"
    )
    expect(region).to_contain_text("00:00 UTC")
    expect(region).to_contain_text("Stored capacity does not reset")
    refresh = region.get_by_role("button", name="Refresh usage")
    refresh.focus()
    page.keyboard.press("Enter")
    expect(refresh).to_be_enabled()
    expect(refresh).to_be_focused()
    assert len(response["calls"]) >= 2
    page.get_by_text("How the allowance works", exact=True).click()
    expect(region).to_contain_text("not an extra bill")
    screenshot(page, output_path, f"normal-{width}")
    assert region.evaluate("el => el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().right <= innerWidth")
    # Existing settings tables can overflow at 320px. The new panel must not
    # increase that baseline, as well as fitting its own viewport above.
    with_panel = page.evaluate("document.documentElement.scrollWidth")
    region.evaluate("el => el.hidden = true")
    without_panel = page.evaluate("document.documentElement.scrollWidth")
    region.evaluate("el => el.hidden = false")
    assert with_panel <= without_panel
    results = Axe().run(
        page,
        context={"include": [["#managed-storage-usage"]]},
        options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}},
    )
    assert not results.response["violations"], results.generate_report()
    screenshot(page, output_path, f"normal-{width}")


@pytest.mark.parametrize("access", ["read_only", "suspended"])
def test_restrictions_and_pending_permissions(ui, access, output_path):
    page, _, _, response = ui
    response["body"]["status"].update(applied_access=access, desired_access="read_write")
    region = open_ui(ui)
    expect(region).to_contain_text("Permission change pending: Read and write")
    expect(region).to_contain_text("read-only" if access == "read_only" else "reported as paused")
    screenshot(page, output_path, access)


def test_near_limit_and_observation_mode(ui, output_path):
    page, _, _, response = ui
    response["body"]["status"]["enforcement_enabled"] = False
    response["body"]["status"]["usage"]["operation_microcents"] = 90000000
    region = open_ui(ui)
    expect(region).to_contain_text("Approaching the monthly activity limit")
    expect(region).to_contain_text("Automatic restrictions are not enabled")
    screenshot(page, output_path, "near-limit-observation")


@pytest.mark.parametrize("phase", ["reserved", "bucket_ready", "token_pending", "activating"])
def test_provisioning_without_usage_does_not_show_zero(ui, phase):
    _, _, _, response = ui
    response["body"]["status"].update(phase=phase, usage=None, stale=True)
    region = open_ui(ui)
    expect(region).to_contain_text("setup is in progress")
    expect(region).to_contain_text("Usage has not been reported")
    expect(region.get_by_role("progressbar")).to_have_count(0)


@pytest.mark.parametrize("mode", ["local", "byo", "legacy"])
def test_unmanaged_storage_has_no_panel_or_usage_requests(ui, mode):
    page, _, state, response = ui
    state["backend"] = "local" if mode == "local" else "s3"
    if mode == "legacy":
        del state["managed_storage_allocation_id"]
    else:
        state["managed_storage_allocation_id"] = None
    region = open_ui(ui)
    expect(page.locator("#archive-backend-table")).to_be_visible()
    expect(region).to_be_hidden()
    assert not response["calls"]


def test_refresh_failure_preserves_snapshot_and_retry_recovers(ui, output_path):
    page, _, _, response = ui
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    response.update(status=503, body={"managed": True, "error": "unavailable", "status": None})
    region.get_by_role("button", name="Refresh usage").click()
    expect(region.get_by_role("status")).to_contain_text("unavailable")
    expect(region).to_contain_text("last reported values")
    expect(region.get_by_role("progressbar", name="Stored capacity")).to_have_attribute("aria-valuenow", "25")
    screenshot(page, output_path, "stale-after-failure")
    response.update(status=200, body={"managed": True, "status": snapshot()})
    region.get_by_role("button", name="Refresh usage").click()
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    expect(region.get_by_text("Usage or access status may be out of date.", exact=False)).to_have_count(0)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_first_fetch_error_is_not_zero_usage(ui, status):
    _, _, _, response = ui
    response.update(status=status, body={"managed": True, "status": None, "error": "secret upstream body"})
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_contain_text("Sign in" if status in (401, 403) else "unavailable")
    expect(region.get_by_role("progressbar")).to_have_count(0)
    expect(region).not_to_contain_text("secret upstream body")
    expect(region.get_by_role("button", name="Refresh usage")).to_be_enabled()


def test_hostile_strings_and_malformed_values_are_not_html(ui):
    page, _, _, response = ui
    response["body"]["status"]["reason"] = '<img src=x onerror="window.storageXss=1">'
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    assert page.evaluate("window.storageXss") is None
    response["body"]["status"]["capacity_bytes"] = "<script>alert(1)</script>"
    region.get_by_role("button", name="Refresh usage").click()
    expect(region.get_by_role("status")).to_contain_text("unavailable")
    expect(region.locator("img,script")).to_have_count(0)


def test_exceeded_allowance_clamps_meter_but_preserves_number(ui):
    _, _, _, response = ui
    response["body"]["status"]["usage"]["operation_microcents"] = 250000000
    region = open_ui(ui)
    bar = region.get_by_role("progressbar", name="Monthly activity allowance")
    expect(bar).to_have_attribute("aria-valuenow", "100")
    expect(bar).to_have_attribute("aria-valuetext", "$2.50 of $1.00 (250%)")


def test_migration_away_discards_inflight_response(ui):
    page, _, _, _ = ui
    pending = []
    page.route("**/api/storage/managed_usage", lambda route: pending.append(route))
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_contain_text("Loading")
    page.evaluate("window.managedStorageUsage.setAllocation(null)")
    assert pending
    pending[0].fulfill(json={"managed": True, "status": snapshot()})
    expect(region).to_be_hidden()


def test_backend_stale_and_ineligible_state_are_visible(ui, output_path):
    page, _, _, response = ui
    response["body"]["status"].update(
        stale=True, reason="ineligible_or_deleted", applied_access="suspended", desired_access="suspended"
    )
    region = open_ui(ui)
    expect(region).to_contain_text("last reported values")
    expect(region).to_contain_text("no longer eligible")
    screenshot(page, output_path, "stale-ineligible")


def test_timeout_releases_refresh_button_and_can_retry(ui):
    page, _, _, response = ui
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    page.evaluate("""() => {
      window.managedStorageUsage.destroy();
      window.managedStorageUsage = createManagedStorageUsage(document.getElementById('managed-storage-usage'), {timeoutMs: 100});
    }""")
    held = []

    def hold(route):
        held.append(route)

    page.route("**/api/storage/managed_usage", hold)
    page.evaluate("window.managedStorageUsage.setAllocation('a'.repeat(32))")
    expect(region.get_by_role("status")).to_contain_text("unavailable")
    expect(region.get_by_role("button", name="Refresh usage")).to_be_enabled()
    page.unroute("**/api/storage/managed_usage", hold)
    region.get_by_role("button", name="Refresh usage").click()
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    for route in held:
        route.abort()


def test_refresh_deduplicates_requests_and_preserves_explanation_focus(ui):
    page, _, _, response = ui
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    region.locator("summary").click()
    region.locator("summary").focus()
    before = len(response["calls"])
    page.evaluate(
        "Promise.all([managedStorageUsage.refresh(), managedStorageUsage.refresh(), managedStorageUsage.refresh()])"
    )
    assert len(response["calls"]) == before + 1
    expect(region.locator("summary")).to_be_focused()
    expect(region.locator("details")).to_have_attribute("open", "")


def test_pagehide_aborts_request_and_pageshow_refreshes(ui):
    page, _, _, _ = ui
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    held = []

    def hold(route):
        held.append(route)

    page.route("**/api/storage/managed_usage", hold)
    region.get_by_role("button", name="Refresh usage").click()
    expect(region.get_by_role("status")).to_contain_text("Refreshing")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    expect(region.get_by_role("progressbar")).to_have_count(0)
    page.unroute("**/api/storage/managed_usage", hold)
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    for route in held:
        route.abort()


def test_new_allocation_ignores_old_response(ui):
    page, _, _, response = ui
    held = []

    def hold(route):
        held.append(route)

    page.route("**/api/storage/managed_usage", hold)
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_contain_text("Loading")
    page.unroute("**/api/storage/managed_usage", hold)
    response["body"]["status"].update(allocation_id="b" * 32, capacity_bytes=200 * 1024**3)
    page.evaluate("window.managedStorageUsage.setAllocation('b'.repeat(32))")
    expect(region).to_contain_text("200 GiB")
    for route in held:
        route.fulfill(json={"managed": True, "status": snapshot()})
    expect(region).to_contain_text("200 GiB")


@pytest.mark.parametrize("status", [401, 403])
def test_lost_owner_authorization_clears_private_snapshot(ui, status):
    _, _, _, response = ui
    region = open_ui(ui)
    expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
    response.update(status=status, body={"error": "unauthorized"})
    region.get_by_role("button", name="Refresh usage").click()
    expect(region.get_by_role("status")).to_contain_text("Sign in as the instance owner")
    expect(region.get_by_role("progressbar")).to_have_count(0)
    expect(region).not_to_contain_text("25 GiB")


def test_usage_loads_from_server_binding_while_object_metadata_is_stalled(ui):
    page, local, _, _ = ui
    held = []
    with sqlite3.connect(local.config.db_path) as db:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket=?, s3_endpoint=?",
            (BINDING["s3_bucket"], BINDING["s3_endpoint"]),
        )
        db.execute("INSERT INTO settings (key, value) VALUES ('managed_storage_binding', ?)", (json.dumps(BINDING),))
    try:
        page.route("**/api/storage/archive_backend", lambda route: held.append(route))
        region = open_ui(ui)
        expect(region.get_by_role("status")).to_have_text("Cloud storage usage updated.")
        expect(page.locator("#archive-backend-status")).to_contain_text("Loading")
        assert held
    finally:
        for route in held:
            route.abort()
        with sqlite3.connect(local.config.db_path) as db:
            db.execute("UPDATE archive_backend SET backend='disabled', s3_bucket=NULL, s3_endpoint=NULL")
            db.execute("DELETE FROM settings WHERE key='managed_storage_binding'")
