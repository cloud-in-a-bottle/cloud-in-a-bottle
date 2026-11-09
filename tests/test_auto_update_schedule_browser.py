import json
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
import requests
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect
from test_accessibility import WCAG_AA_TAGS

from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.utils import managed_router

WINTER = datetime.fromisoformat("2026-01-15T12:00:00+00:00")
SUMMER = datetime.fromisoformat("2026-07-15T12:00:00+00:00")


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalStack]:
    config = make_local_stack_config(
        data_root_dir=str(tmp_path_factory.mktemp("auto-update-schedule")),
        port=_unused_port(),
        zone_name="autoupdatesched",
        default_apps=[],
    )
    with managed_router(config):
        yield LocalStack(config=config)


@pytest.fixture(scope="module")
def owner(stack: LocalStack) -> requests.Session:
    return complete_setup(stack)


def _set_settings(stack: LocalStack, values: dict[str, str | None]) -> None:
    with closing(sqlite3.connect(stack.config.db_path)) as db, db:
        for key, value in values.items():
            if value is None:
                db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def _get_setting(stack: LocalStack, key: str) -> str | None:
    with closing(sqlite3.connect(stack.config.db_path)) as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


@pytest.fixture(autouse=True)
def schedule(stack: LocalStack, owner: requests.Session) -> None:
    _set_settings(
        stack,
        {
            "auto_update_enabled": "1",
            "auto_update_time": "06:00",
            "auto_update_timezone": "America/Chicago",
            "auto_update_last_run_at": None,
            "auto_update_last_run_outcome": None,
            "auto_update_last_run_detail": None,
        },
    )


def _fake_system_agent(route: Route, channel: str = "tags") -> None:
    # The local stack has no system agent; answer the update calls the settings page makes on load.
    if route.request.url.endswith("/api/settings/get-remote"):
        route.fulfill(
            json={"url": "https://github.com/cloud-in-a-bottle/cloud-in-a-bottle", "ref": "v1.0.0", "channel": channel}
        )
    else:
        route.fulfill(json={"state": "UP_TO_DATE"})


@pytest.fixture
def open_settings(
    browser: Browser, stack: LocalStack, owner: requests.Session
) -> Iterator[tuple[list[BrowserContext], list[str]]]:
    contexts: list[BrowserContext] = []
    errors: list[str] = []
    yield contexts, errors
    for context in contexts:
        context.close()
    assert not errors, errors


def _open(
    fixture: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
    zone: str,
    now: datetime = WINTER,
    init_script: str | None = None,
    channel: str = "tags",
) -> Page:
    contexts, errors = fixture
    context = browser.new_context(timezone_id=zone, locale="en-US")
    contexts.append(context)
    context.add_cookies([{"name": c.name, "value": c.value, "url": stack.router_url} for c in owner.cookies])
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    if init_script:
        page.add_init_script(init_script)
    page.clock.set_fixed_time(now)
    page.route("**/api/settings/get-remote", lambda route: _fake_system_agent(route, channel))
    page.route("**/api/settings/update", _fake_system_agent)
    response = page.goto(f"{stack.router_url}/settings")
    assert response is not None and response.ok
    expect(page.locator("#auto-update-time")).not_to_have_value("")
    return page


def _option_values(page: Page) -> list[str]:
    return page.locator("#auto-update-zone option").evaluate_all("options => options.map(o => o.value)")


def _save(page: Page, action: str, value: str) -> dict[str, object]:
    with page.expect_response(
        lambda r: r.url.endswith("/api/settings/auto-update") and r.request.method == "POST"
    ) as r:
        if action == "time":
            page.locator("#auto-update-time").fill(value)
            page.locator("#auto-update-time").dispatch_event("change")
        else:
            page.locator("#auto-update-zone").select_option(value=value)
    expect(page.locator("#auto-update-msg")).to_have_text("Saved.")
    body: dict[str, object] = json.loads(r.value.request.post_data or "")
    return body


@pytest.mark.parametrize(
    ("browser_zone", "now"),
    [("America/Chicago", WINTER), ("America/Chicago", SUMMER), ("Asia/Tokyo", WINTER), ("UTC", SUMMER)],
)
def test_shows_the_saved_time_and_timezone_as_is(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
    browser_zone: str,
    now: datetime,
) -> None:
    page = _open(open_settings, browser, stack, owner, browser_zone, now)
    expect(page.locator("#auto-update-time")).to_have_value("06:00")
    expect(page.locator("#auto-update-zone")).to_have_value("America/Chicago")


def test_dropdown_lists_every_timezone(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo")
    zone = page.locator("#auto-update-zone")
    expect(zone).to_have_accessible_name("Timezone")
    values = _option_values(page)
    assert values == sorted(set(values))
    assert len(values) > 300
    assert {"UTC", "America/Chicago", "Europe/London"} <= set(values)
    expect(zone.locator("option[value='Asia/Tokyo']")).to_have_text("Asia/Tokyo (this browser)")
    expect(zone.locator("option[value='America/Chicago']")).to_have_text("America/Chicago")


def test_saves_the_time_and_timezone_as_entered(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    # The browser's own timezone plays no part.
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo")
    assert _save(page, "time", "07:15") == {"enabled": True, "time": "07:15", "timezone": "America/Chicago"}
    assert _get_setting(stack, "auto_update_time") == "07:15"
    assert _save(page, "zone", "Europe/Berlin") == {"enabled": True, "time": "07:15", "timezone": "Europe/Berlin"}
    assert _get_setting(stack, "auto_update_timezone") == "Europe/Berlin"
    expect(page.locator("#auto-update-time")).to_have_value("07:15")

    # Another browser, elsewhere, sees the same schedule.
    other = _open(open_settings, browser, stack, owner, "America/Los_Angeles", SUMMER)
    expect(other.locator("#auto-update-time")).to_have_value("07:15")
    expect(other.locator("#auto-update-zone")).to_have_value("Europe/Berlin")


def test_failed_save_puts_back_the_saved_schedule(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo")
    page.route(
        "**/api/settings/auto-update",
        lambda route: (
            route.fulfill(status=500, json={"detail": "boom"}) if route.request.method == "POST" else route.fallback()
        ),
    )
    page.locator("#auto-update-zone").select_option(value="Europe/Berlin")
    expect(page.locator("#auto-update-msg")).to_contain_text("Failed to save automatic update settings")
    expect(page.locator("#auto-update-zone")).to_have_value("America/Chicago")
    page.locator("#auto-update-time").fill("08:00")
    page.locator("#auto-update-time").dispatch_event("change")
    expect(page.locator("#auto-update-time")).to_have_value("06:00")
    assert _get_setting(stack, "auto_update_timezone") == "America/Chicago"


def test_a_saved_timezone_the_browser_names_differently_is_still_shown(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    # Saved from a browser that uses the current name; Chromium lists the zone as Asia/Calcutta.
    _set_settings(stack, {"auto_update_timezone": "Asia/Kolkata"})
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo")
    expect(page.locator("#auto-update-zone")).to_have_value("Asia/Kolkata")
    values = _option_values(page)
    assert values == sorted(set(values))


def test_without_a_zone_list_offers_the_saved_zone_this_browsers_and_utc(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo", init_script="delete Intl.supportedValuesOf;")
    expect(page.locator("#auto-update-zone option")).to_have_text(
        ["America/Chicago", "Asia/Tokyo (this browser)", "UTC"]
    )
    assert _save(page, "zone", "Asia/Tokyo")["timezone"] == "Asia/Tokyo"


def test_dropdown_is_disabled_with_the_time(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    _set_settings(stack, {"auto_update_enabled": "0"})
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo")
    expect(page.locator("#auto-update-time")).to_be_disabled()
    expect(page.locator("#auto-update-zone")).to_be_disabled()
    page.locator("#auto-update-enabled").check()
    expect(page.locator("#auto-update-msg")).to_have_text("Saved.")
    expect(page.locator("#auto-update-time")).to_be_enabled()
    expect(page.locator("#auto-update-zone")).to_be_enabled()
    assert _get_setting(stack, "auto_update_timezone") == "America/Chicago"

    # On a branch remote the whole row is paused.
    paused = _open(open_settings, browser, stack, owner, "Asia/Tokyo", channel="branch")
    expect(paused.locator("#auto-update-paused")).to_be_visible()
    expect(paused.locator("#auto-update-zone")).to_be_disabled()


def test_last_run_is_shown_in_the_schedules_timezone(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    _set_settings(
        stack,
        {"auto_update_last_run_at": "2026-01-15T12:00:00+00:00", "auto_update_last_run_outcome": "up_to_date"},
    )
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo")
    expect(page.locator("#auto-update-last")).to_have_text(
        "Last automatic update: 1/15/2026, 6:00:00 AM CST. Already up to date."
    )


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"enabled": True, "time": "06:00", "timezone": "Mars/Olympus_Mons"}, "invalid_timezone"),
        ({"enabled": True, "time": "06:00", "timezone": "CST"}, "invalid_timezone"),
        ({"enabled": True, "time": "06:00", "timezone": ""}, "invalid_timezone"),
        ({"enabled": True, "time": "6am", "timezone": "America/Chicago"}, "invalid_time"),
        ({"enabled": True, "time": "25:00", "timezone": "America/Chicago"}, "invalid_time"),
    ],
)
def test_api_rejects_a_bad_schedule(
    stack: LocalStack, owner: requests.Session, body: dict[str, object], code: str
) -> None:
    resp = owner.post(f"{stack.router_url}/api/settings/auto-update", json=body, timeout=10)
    assert resp.status_code == 400, resp.text
    assert code in resp.text
    assert _get_setting(stack, "auto_update_timezone") == "America/Chicago"
    assert _get_setting(stack, "auto_update_time") == "06:00"


def test_time_and_timezone_share_a_line(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "America/Chicago")
    page.set_viewport_size({"width": 1280, "height": 900})
    boxes = [
        page.locator(sel).bounding_box() for sel in ("#auto-update-enabled", "#auto-update-time", "#auto-update-zone")
    ]
    centres = [box["y"] + box["height"] / 2 for box in boxes if box is not None]
    assert len(centres) == 3 and max(centres) - min(centres) < 4, boxes


def test_time_and_timezone_are_styled_alike(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "America/Chicago")
    props = ["borderTopColor", "borderTopWidth", "borderTopLeftRadius", "backgroundColor", "fontSize", "height"]
    script = "(el, props) => props.map(p => getComputedStyle(el)[p])"
    assert page.locator("#auto-update-time").evaluate(script, props) == page.locator("#auto-update-zone").evaluate(
        script, props
    )


def test_schedule_row_fits_a_phone(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "America/Chicago")
    page.set_viewport_size({"width": 375, "height": 800})
    row = page.locator("#auto-update-row").bounding_box()
    zone = page.locator("#auto-update-zone").bounding_box()
    assert row is not None and zone is not None
    assert zone["x"] + zone["width"] <= row["x"] + row["width"] + 1, (row, zone)


def test_auto_update_section_has_no_wcag_violations(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
    output_path: str,
) -> None:
    _set_settings(
        stack,
        {"auto_update_last_run_at": "2026-01-15T12:00:00+00:00", "auto_update_last_run_outcome": "up_to_date"},
    )
    page = _open(open_settings, browser, stack, owner, "America/Chicago")
    expect(page.locator("#auto-update-last")).to_be_visible()
    results = Axe().run(
        page,
        context={"include": [["#auto-update-row"], ["#auto-update-last"]]},
        options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}, "resultTypes": ["violations"]},
    )
    assert results.response["violations"] == []
    page.locator("#auto-update-row").scroll_into_view_if_needed()
    page.locator("#auto-update-row").locator("xpath=..").screenshot(
        path=str(Path(output_path) / "auto-update-schedule.png")
    )
