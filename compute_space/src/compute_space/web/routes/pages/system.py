from typing import Any
from typing import AnyStr

from litestar import Router
from litestar import WebSocket
from litestar import get
from litestar import websocket
from litestar.exceptions import NotAuthorizedException
from litestar.response import Template

from compute_space.core.terminal import handle_terminal_ws
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import verify_owner_auth


@get("/system/", guards=[require_owner_auth])
async def system_page() -> Template:
    """Serve the System Info page (security audit, storage, logs)."""
    return Template(template_name="system.html")


@get("/diagnostics/", guards=[require_owner_auth])
async def diagnostics_page() -> Template:
    """Serve the Diagnostics page (copyable/downloadable debug bundle)."""
    return Template(template_name="diagnostics.html")


@get("/terminal/", guards=[require_owner_auth])
async def terminal_page() -> Template:
    """Serve the web terminal UI."""
    return Template(template_name="terminal.html")


class _LitestarTerminalAdapter:
    """Adapt a Litestar WebSocket to the framework-neutral ``TerminalWebsocket``
    Protocol that ``handle_terminal_ws`` expects."""

    def __init__(self, ws: WebSocket[Any, Any, Any]) -> None:
        self._ws = ws

    async def send(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def receive(self) -> AnyStr:
        data: AnyStr = await self._ws.receive_bytes()  # type: ignore[assignment]
        return data


@websocket("/terminal/ws")
async def terminal_ws(socket: WebSocket[Any, Any, Any]) -> None:
    """WebSocket endpoint for the terminal PTY bridge.

    Guards currently only signal failure via HTTPException, so the auth check is
    inline: accept first so the client gets a clean close on failure.
    """
    try:
        verify_owner_auth(socket)
    except NotAuthorizedException:
        await socket.accept()
        await socket.close(code=4401, reason="Missing or invalid authorization")
        return
    await socket.accept()
    await handle_terminal_ws(_LitestarTerminalAdapter(socket))


# excluded due to user-facing html page rather than api
pages_system_routes = Router(
    path="/",
    route_handlers=[system_page, diagnostics_page, terminal_page, terminal_ws],
    include_in_schema=False,
)
