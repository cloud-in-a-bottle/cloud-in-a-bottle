"""Automated WCAG checks for representative Cloud in a Bottle UI pages."""

import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest
from axe_playwright_python.sync_playwright import Axe
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


@pytest.mark.parametrize(("initial_status", "width"), [("building", 1280), ("starting", 390)])
def test_starting_app_keeps_details_accessible_and_updates_launch_link(
    page: Page, stack: LocalStack, initial_status: str, width: int
) -> None:
    owner = complete_setup(stack)
    with closing(sqlite3.connect(stack.config.db_path)) as db, db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('startingtest', 'startup-test', '1.0', '/tmp/startup-test', 29999, ?)",
            (initial_status,),
        )
    page.context.add_cookies(
        [{"name": cookie.name, "value": cookie.value, "url": stack.router_url} for cookie in owner.cookies]
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{stack.router_url}/dashboard")
    row = page.locator('[data-app-id="startingtest"]')
    launch = row.locator(".app-row__name")
    status = row.locator(".app-row__status")
    details = row.get_by_role("link", name="Details")
    original_url = launch.get_attribute("data-app-url")
    assert original_url is not None
    expect(launch).to_be_disabled()
    assert launch.get_attribute("href") is None
    expect(status).to_have_text(f"{initial_status.capitalize()}...")
    expect(status).to_have_class("app-row__status")

    # The unavailable launch is skipped by Tab; Details still gets focus.
    page.get_by_role("link", name="+ New", exact=True).focus()
    page.keyboard.press("Tab")
    expect(details).to_be_focused()
    page.keyboard.press("Enter")
    page.wait_for_url(f"{stack.router_url}/app_detail/startup-test")
    page.goto(f"{stack.router_url}/dashboard")
    row.hover()
    details.click()
    page.wait_for_url(f"{stack.router_url}/app_detail/startup-test")
    page.goto(f"{stack.router_url}/dashboard")

    # Exercise real /api/apps polling, without reloading the dashboard.
    for next_status in ("starting", "running", "building", "error", "stopped", "removing"):
        with closing(sqlite3.connect(stack.config.db_path)) as db, db:
            db.execute("UPDATE apps SET status = ? WHERE app_id = 'startingtest'", (next_status,))
        expect(row).to_have_attribute("data-status", next_status, timeout=10000)
        if next_status in ("building", "starting"):
            expect(launch).to_be_disabled()
            assert launch.get_attribute("href") is None
            expect(status).to_have_text(f"{next_status.capitalize()}...")
            expect(status).to_have_class("app-row__status")
        else:
            expect(launch).to_be_enabled()
            expect(launch).to_have_attribute("href", original_url)
            expect(launch).to_have_attribute("target", "_blank")
            expect(status).to_have_text(next_status)
            expect(status).to_have_class("app-row__status visually-hidden")
        expect(details).to_have_attribute("href", "/app_detail/startup-test")
