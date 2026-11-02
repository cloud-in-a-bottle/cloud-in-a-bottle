"""Run the real terminal page in Chromium against a local router, using only the instance's own assets."""

import json
import socket
import time
from collections.abc import Iterator

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import ViewportSize
from playwright.sync_api import WebSocket
from playwright.sync_api import expect

from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import complete_setup
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.utils import managed_router

VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
VENDORED = ("/static/vendor/xterm/xterm.js", "/static/vendor/xterm/xterm.css", "/static/vendor/addon-fit/addon-fit.js")
# Mirrors terminal.js: the terminal's own font wait, after which it opens with whatever font is available.
FONT_WAIT_MS = 3000

# Row height and column count xterm itself computes for a font stack, using a throwaway terminal sized like the page's.
_MEASURE_WITH = """(fontFamily) => {
    const frame = document.getElementById('terminal');
    const host = document.createElement('div');
    host.style.cssText = `position:absolute; visibility:hidden; width:${frame.clientWidth}px; height:${frame.clientHeight}px`;
    document.body.appendChild(host);
    const term = new Terminal({fontSize: 14, fontFamily});
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(host);
    const dims = fit.proposeDimensions();
    const rowHeight = host.querySelector('.xterm-rows > div').getBoundingClientRect().height;
    term.dispose();
    host.remove();
    return {rowHeight, cols: dims.cols, rows: dims.rows};
}"""


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalStack]:
    config = make_local_stack_config(
        data_root_dir=str(tmp_path_factory.mktemp("terminal")),
        port=_unused_port(),
        zone_name="terminal",
        default_apps=[],
    )
    local_stack = LocalStack(config=config)
    with managed_router(config):
        yield local_stack


@pytest.fixture(scope="module")
def owner(stack: LocalStack) -> requests.Session:
    session: requests.Session = complete_setup(stack)
    return session


@pytest.fixture
def owner_page(page: Page, stack: LocalStack, owner: requests.Session) -> Page:
    page.set_viewport_size(VIEWPORT)
    page.context.add_cookies(
        [{"name": c.name, "value": c.value or "", "url": stack.router_url} for c in owner.cookies]
    )
    return page


def _resize_frames(page: Page) -> list[dict[str, int]]:
    frames: list[dict[str, int]] = []

    def on_websocket(ws: WebSocket) -> None:
        def on_frame(payload: str | bytes) -> None:
            if isinstance(payload, bytes) and payload[:1] == b"\x01":
                frames.append(json.loads(payload[1:]))

        ws.on("framesent", on_frame)

    page.on("websocket", on_websocket)
    return frames


def _run_command(page: Page) -> None:
    screen = page.locator(".xterm-screen")
    expect(screen).to_be_visible()
    screen.click()
    # The marker only appears once bash has evaluated it, so the echoed command line can't satisfy the check.
    page.keyboard.type("echo terminal-$((6 * 7))-ok\n")
    expect(page.locator(".xterm-rows")).to_contain_text("terminal-42-ok", timeout=15000)


def _plex_mono_loaded(page: Page) -> bool:
    return bool(
        page.evaluate(
            """() => [...document.fonts].some(
                f => f.family.replace(/["']/g, '') === 'IBM Plex Mono' && f.status === 'loaded')"""
        )
    )


def test_terminal_works_with_only_same_origin_requests(owner_page: Page, stack: LocalStack) -> None:
    page = owner_page
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    responses: dict[str, int] = {}
    page.on("response", lambda response: responses.__setitem__(response.url.split("?", 1)[0], response.status))
    sockets: list[str] = []
    page.on("websocket", lambda ws: sockets.append(ws.url))

    page.goto(f"{stack.router_url}/terminal/")
    _run_command(page)

    origin = stack.router_url
    assert requests and all(url.startswith(f"{origin}/") for url in requests), [
        url for url in requests if not url.startswith(f"{origin}/")
    ]
    ws_origin = origin.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    assert sockets == [f"{ws_origin}/terminal/ws"]
    for path in VENDORED:
        assert responses.get(f"{origin}{path}") == 200, (path, responses)
    font_responses = {url: status for url, status in responses.items() if "/static/fonts/ibm-plex-mono-" in url}
    assert font_responses and set(font_responses.values()) == {200}
    assert _plex_mono_loaded(page)
    # xterm.css is applied (it hides the helper textarea that receives keystrokes off-screen).
    textarea = page.locator(".xterm-helper-textarea")
    assert textarea.evaluate("el => getComputedStyle(el).position") == "absolute"
    assert textarea.evaluate("el => getComputedStyle(el).opacity") == "0"


def test_terminal_grid_is_measured_with_the_web_font(owner_page: Page, stack: LocalStack) -> None:
    page = owner_page
    resizes = _resize_frames(page)
    page.goto(f"{stack.router_url}/terminal/")
    _run_command(page)

    font_stack = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim()")
    assert font_stack.startswith('"IBM Plex Mono"'), font_stack
    fallback_stack = font_stack.split(",", 1)[1].strip()
    with_plex = page.evaluate(_MEASURE_WITH, font_stack)
    with_fallback = page.evaluate(_MEASURE_WITH, fallback_stack)
    # Otherwise this test couldn't tell the fonts apart on this machine.
    assert (with_plex["rowHeight"], with_plex["rows"]) != (with_fallback["rowHeight"], with_fallback["rows"])

    row_height = page.locator(".xterm-rows > div").first.evaluate("el => el.getBoundingClientRect().height")
    assert row_height == with_plex["rowHeight"]
    # The size sent to the shell must be the web font's grid too, or full-screen programs wrap wrongly.
    assert resizes and resizes[0] == {"type": "resize", "cols": with_plex["cols"], "rows": with_plex["rows"]}


def test_stalled_font_does_not_keep_the_terminal_closed(owner_page: Page, stack: LocalStack) -> None:
    page = owner_page
    stalled: list[Route] = []
    page.route("**/static/fonts/ibm-plex-mono-*/**", lambda route: stalled.append(route))  # Never answered.

    started = time.monotonic()
    page.goto(f"{stack.router_url}/terminal/", wait_until="domcontentloaded")
    _run_command(page)
    elapsed_ms = (time.monotonic() - started) * 1000

    assert stalled
    assert not _plex_mono_loaded(page)
    assert elapsed_ms >= FONT_WAIT_MS - 500  # It did wait for the font...
    assert elapsed_ms < FONT_WAIT_MS + 10000  # ...but only for a bounded time.


def test_failed_font_opens_the_terminal_without_waiting(owner_page: Page, stack: LocalStack) -> None:
    page = owner_page
    page.route("**/static/fonts/ibm-plex-mono-*/**", lambda route: route.abort("connectionrefused"))

    started = time.monotonic()
    page.goto(f"{stack.router_url}/terminal/", wait_until="domcontentloaded")
    expect(page.locator(".xterm-screen")).to_be_visible()
    elapsed_ms = (time.monotonic() - started) * 1000

    assert elapsed_ms < FONT_WAIT_MS
    _run_command(page)
