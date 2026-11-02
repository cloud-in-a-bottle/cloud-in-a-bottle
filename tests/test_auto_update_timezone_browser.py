import json
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path

import attr
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
PREFIX = "The time is in your browser's timezone: "
DST_NOTE = ", so the local time shown here shifts when daylight saving time starts or ends."
# Chromium's ICU still reports some zones by their older names.
ALIASES = {"Asia/Kolkata": "Asia/Calcutta", "Asia/Kathmandu": "Asia/Katmandu"}


@attr.s(auto_attribs=True, frozen=True)
class TimezoneCase:
    zone: str
    now: datetime
    # What the time input shows for a schedule saved as 09:00 UTC.
    local_time: str
    # The message after the zone name; {zone} is the name the browser reports.
    message: str


CASES = [
    TimezoneCase(
        "America/New_York",
        WINTER,
        "04:00",
        "Eastern Standard Time ({zone}, UTC-05:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    TimezoneCase(
        "America/New_York",
        SUMMER,
        "05:00",
        "Eastern Daylight Time ({zone}, UTC-04:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    TimezoneCase(
        "America/Los_Angeles",
        SUMMER,
        "02:00",
        "Pacific Daylight Time ({zone}, UTC-07:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    TimezoneCase(
        "America/St_Johns",
        WINTER,
        "05:30",
        "Newfoundland Standard Time ({zone}, UTC-03:30). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    # Southern hemisphere: daylight saving time is in January.
    TimezoneCase(
        "Australia/Sydney",
        WINTER,
        "20:00",
        "Australian Eastern Daylight Time ({zone}, UTC+11:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    TimezoneCase(
        "Australia/Sydney",
        SUMMER,
        "19:00",
        "Australian Eastern Standard Time ({zone}, UTC+10:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    # Shifts by half an hour for daylight saving time.
    TimezoneCase(
        "Australia/Lord_Howe",
        WINTER,
        "20:00",
        "Lord Howe Daylight Time ({zone}, UTC+11:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    TimezoneCase(
        "Asia/Kolkata",
        SUMMER,
        "14:30",
        "India Standard Time ({zone}, UTC+05:30). It is saved as 09:00 UTC.",
    ),
    TimezoneCase(
        "Asia/Kathmandu",
        WINTER,
        "14:45",
        "Nepal Time ({zone}, UTC+05:45). It is saved as 09:00 UTC.",
    ),
    TimezoneCase(
        "Pacific/Kiritimati",
        WINTER,
        "23:00",
        "Line Islands Time ({zone}, UTC+14:00). It is saved as 09:00 UTC.",
    ),
    TimezoneCase(
        "Pacific/Pago_Pago",
        WINTER,
        "22:00",
        "American Samoa Standard Time ({zone}, UTC-11:00). It is saved as 09:00 UTC.",
    ),
    # At UTC+0 in winter, but the zone still shifts in summer, so the UTC time is still worth showing.
    TimezoneCase(
        "Europe/London",
        WINTER,
        "09:00",
        "Greenwich Mean Time ({zone}, UTC). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    TimezoneCase(
        "Europe/London",
        SUMMER,
        "10:00",
        "British Summer Time ({zone}, UTC+01:00). It is saved as 09:00 UTC" + DST_NOTE,
    ),
    # Always at UTC+0: repeating the UTC time adds nothing.
    TimezoneCase("Africa/Abidjan", SUMMER, "09:00", "Greenwich Mean Time ({zone}, UTC)."),
    TimezoneCase("UTC", SUMMER, "09:00", "Coordinated Universal Time (UTC)."),
    # A zone with no readable name falls back to the IANA name and offset.
    TimezoneCase("Etc/GMT+5", WINTER, "04:00", "{zone} (UTC-05:00). It is saved as 09:00 UTC."),
]


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalStack]:
    config = make_local_stack_config(
        data_root_dir=str(tmp_path_factory.mktemp("auto-update-timezone")),
        port=_unused_port(),
        zone_name="autoupdatetz",
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
            "auto_update_time_utc": "09:00",
            "auto_update_last_run_at": None,
            "auto_update_last_run_outcome": None,
            "auto_update_last_run_detail": None,
        },
    )


def _fake_system_agent(route: Route) -> None:
    # The local stack has no system agent; answer the update calls the settings page makes on load.
    if route.request.url.endswith("/api/settings/get-remote"):
        route.fulfill(
            json={"url": "https://github.com/cloud-in-a-bottle/cloud-in-a-bottle", "ref": "v1.0.0", "channel": "tags"}
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
    now: datetime,
    init_script: str | None = None,
) -> Page:
    contexts, errors = fixture
    context = browser.new_context(timezone_id=zone, locale="en-US")
    contexts.append(context)
    context.add_cookies([{"name": c.name, "value": c.value, "url": stack.router_url} for c in owner.cookies])
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    if init_script:
        # Before the fake clock, which wraps Intl.DateTimeFormat and would hide later patches to it.
        page.add_init_script(init_script)
    page.clock.set_fixed_time(now)
    page.route("**/api/settings/get-remote", _fake_system_agent)
    page.route("**/api/settings/update", _fake_system_agent)
    response = page.goto(f"{stack.router_url}/settings")
    assert response is not None and response.ok
    # The checkbox is enabled once both the remote and the schedule have loaded.
    expect(page.locator("#auto-update-enabled")).to_be_enabled()
    return page


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.zone}-{case.now.strftime('%b')}")
def test_names_the_browser_timezone(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
    case: TimezoneCase,
) -> None:
    page = _open(open_settings, browser, stack, owner, case.zone, case.now)
    reported = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert reported in (case.zone, ALIASES.get(case.zone))
    message = PREFIX + case.message.format(zone=reported)
    expect(page.locator("#auto-update-time")).to_have_value(case.local_time)
    expect(page.locator("#auto-update-timezone")).to_have_text(message)
    expect(page.locator("#auto-update-time")).to_have_accessible_description(message)


def test_saving_converts_to_utc_and_updates_the_message(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "America/Los_Angeles", WINTER)
    expect(page.locator("#auto-update-time")).to_have_value("01:00")
    with page.expect_response("**/api/settings/auto-update") as saved:
        page.locator("#auto-update-time").fill("07:15")
        page.locator("#auto-update-time").dispatch_event("change")
    assert json.loads(saved.value.request.post_data or "") == {"enabled": True, "time_utc": "15:15"}
    expect(page.locator("#auto-update-msg")).to_have_text("Saved.")
    expect(page.locator("#auto-update-timezone")).to_have_text(
        PREFIX + "Pacific Standard Time (America/Los_Angeles, UTC-08:00). It is saved as 15:15 UTC" + DST_NOTE
    )
    assert _get_setting(stack, "auto_update_time_utc") == "15:15"

    # The same saved schedule, seen from a browser in another timezone.
    other = _open(open_settings, browser, stack, owner, "Asia/Tokyo", WINTER)
    expect(other.locator("#auto-update-time")).to_have_value("00:15")
    expect(other.locator("#auto-update-timezone")).to_have_text(
        PREFIX + "Japan Standard Time (Asia/Tokyo, UTC+09:00). It is saved as 15:15 UTC."
    )


def test_failed_save_keeps_the_saved_time_in_the_message(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo", WINTER)
    page.route(
        "**/api/settings/auto-update",
        lambda route: (
            route.fulfill(status=500, json={"detail": "boom"}) if route.request.method == "POST" else route.fallback()
        ),
    )
    page.locator("#auto-update-time").fill("08:00")
    page.locator("#auto-update-time").dispatch_event("change")
    expect(page.locator("#auto-update-msg")).to_contain_text("Failed to save automatic update settings")
    expect(page.locator("#auto-update-time")).to_have_value("18:00")
    expect(page.locator("#auto-update-timezone")).to_have_text(
        PREFIX + "Japan Standard Time (Asia/Tokyo, UTC+09:00). It is saved as 09:00 UTC."
    )
    assert _get_setting(stack, "auto_update_time_utc") == "09:00"


def test_message_stays_visible_while_disabled_or_paused(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    _set_settings(stack, {"auto_update_enabled": "0"})
    page = _open(open_settings, browser, stack, owner, "Asia/Tokyo", WINTER)
    expect(page.locator("#auto-update-enabled")).not_to_be_checked()
    expect(page.locator("#auto-update-time")).to_be_disabled()
    expect(page.locator("#auto-update-timezone")).to_be_visible()
    expect(page.locator("#auto-update-timezone")).to_have_text(
        PREFIX + "Japan Standard Time (Asia/Tokyo, UTC+09:00). It is saved as 09:00 UTC."
    )


NO_ZONE_ID = """
  const resolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
  Intl.DateTimeFormat.prototype.resolvedOptions = function () {
    return {...resolvedOptions.call(this), timeZone: undefined};
  };
"""
NO_ZONE_NAME = """
  Intl.DateTimeFormat.prototype.formatToParts = function () { throw new TypeError('unsupported'); };
"""


@pytest.mark.parametrize(
    ("init_script", "message"),
    [
        (NO_ZONE_ID, "Central Standard Time (UTC-06:00). It is saved as 09:00 UTC" + DST_NOTE),
        (NO_ZONE_NAME, "America/Chicago (UTC-06:00). It is saved as 09:00 UTC" + DST_NOTE),
        (NO_ZONE_ID + NO_ZONE_NAME, "UTC-06:00. It is saved as 09:00 UTC" + DST_NOTE),
    ],
    ids=["no-zone-id", "no-zone-name", "neither"],
)
def test_falls_back_when_the_browser_reports_less(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
    init_script: str,
    message: str,
) -> None:
    page = _open(open_settings, browser, stack, owner, "America/Chicago", WINTER, init_script=init_script)
    expect(page.locator("#auto-update-time")).to_have_value("03:00")
    expect(page.locator("#auto-update-timezone")).to_have_text(PREFIX + message)


def test_last_run_names_the_timezone(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
) -> None:
    _set_settings(
        stack,
        {"auto_update_last_run_at": "2026-01-15T09:00:00+00:00", "auto_update_last_run_outcome": "up_to_date"},
    )
    page = _open(open_settings, browser, stack, owner, "America/New_York", WINTER)
    expect(page.locator("#auto-update-last")).to_have_text(
        "Last automatic update: 1/15/2026, 4:00:00 AM EST. Already up to date."
    )


def test_without_javascript_the_hint_still_says_whose_timezone(
    browser: Browser, stack: LocalStack, owner: requests.Session
) -> None:
    with browser.new_context(java_script_enabled=False) as context:
        context.add_cookies([{"name": c.name, "value": c.value, "url": stack.router_url} for c in owner.cookies])
        page = context.new_page()
        page.goto(f"{stack.router_url}/settings")
        expect(page.locator("#auto-update-timezone")).to_have_text("The time is in your browser's timezone.")


def test_auto_update_section_has_no_wcag_violations(
    open_settings: tuple[list[BrowserContext], list[str]],
    browser: Browser,
    stack: LocalStack,
    owner: requests.Session,
    output_path: str,
) -> None:
    _set_settings(
        stack,
        {"auto_update_last_run_at": "2026-01-15T09:00:00+00:00", "auto_update_last_run_outcome": "up_to_date"},
    )
    page = _open(open_settings, browser, stack, owner, "America/New_York", WINTER)
    expect(page.locator("#auto-update-last")).to_be_visible()
    results = Axe().run(
        page,
        context={"include": [["#auto-update-row"], ["#auto-update-timezone"], ["#auto-update-last"]]},
        options={"runOnly": {"type": "tag", "values": WCAG_AA_TAGS}, "resultTypes": ["violations"]},
    )
    assert results.response["violations"] == []
    page.locator("#auto-update-row").scroll_into_view_if_needed()
    page.locator("#auto-update-row").locator("xpath=..").screenshot(
        path=str(Path(output_path) / "auto-update-timezone.png")
    )
