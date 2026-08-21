import asyncio
from typing import Any
from typing import cast

from litestar import WebSocket
from litestar.datastructures import Headers
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve

from compute_space.core.updates import initialize_shutdown_event
from compute_space.web.helpers.proxy import proxy_websocket_request


def _init_shutdown_event() -> None:
    """The proxy races its pumps against wait_for_shutdown(), which is an instant no-op unless the
    app-startup event is wired — without this every proxied session tears down immediately."""
    initialize_shutdown_event(asyncio.Event())


class FakeClientWebSocket:
    """Stand-in for the litestar client-side WebSocket: records what the proxy sends/closes."""

    def __init__(self) -> None:
        self.scope = {
            "type": "websocket",
            "raw_path": b"/ws",
            "query_string": b"",
        }
        self.headers = Headers({})
        self.accepted = False
        self.received: list[bytes | str] = []
        self.closed: tuple[int, str] | None = None
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def accept(self, subprotocols: str | None = None) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        return await self._inbound.get()

    def queue_disconnect(self, code: int) -> None:
        self._inbound.put_nowait({"type": "websocket.disconnect", "code": code})

    async def send_bytes(self, data: bytes) -> None:
        self.received.append(data)

    async def send_text(self, data: str) -> None:
        self.received.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason or "")


def test_backend_close_code_and_reason_reach_client() -> None:
    async def run() -> None:
        _init_shutdown_event()

        async def handler(ws: ServerConnection) -> None:
            await ws.send(b"hello")
            await ws.close(code=4404, reason="Document deleted")

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = FakeClientWebSocket()
            await proxy_websocket_request(cast(WebSocket[Any, Any, Any], client), port)
            assert client.accepted
            assert client.received == [b"hello"]
            assert client.closed == (4404, "Document deleted")

    asyncio.run(run())


def test_backend_abnormal_close_maps_to_1011() -> None:
    async def run() -> None:
        _init_shutdown_event()

        async def handler(ws: ServerConnection) -> None:
            # Kill the TCP connection without a close frame -> abnormal closure (1006).
            transport = ws.transport
            assert transport is not None
            transport.abort()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = FakeClientWebSocket()
            await proxy_websocket_request(cast(WebSocket[Any, Any, Any], client), port)
            assert client.closed is not None
            assert client.closed[0] == 1011

    asyncio.run(run())


def test_client_close_code_reaches_backend() -> None:
    async def run() -> None:
        _init_shutdown_event()
        backend_saw: list[int | None] = []
        connected = asyncio.Event()
        recorded = asyncio.Event()

        async def handler(ws: ServerConnection) -> None:
            connected.set()
            await ws.wait_closed()
            backend_saw.append(ws.close_code)
            recorded.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = FakeClientWebSocket()

            async def disconnect_after_connect() -> None:
                await connected.wait()
                client.queue_disconnect(code=4001)

            await asyncio.gather(
                proxy_websocket_request(cast(WebSocket[Any, Any, Any], client), port),
                disconnect_after_connect(),
            )
            # The proxy can return before the backend handler observes the close.
            await asyncio.wait_for(recorded.wait(), timeout=5)
            assert backend_saw == [4001]

    asyncio.run(run())


def _headers_backend_sees(client_headers: dict[str, str], *, strip_authorization: bool) -> dict[str, str]:
    """Run one proxied WS handshake and return the (lowercased) headers the backend received."""

    async def run() -> dict[str, str]:
        _init_shutdown_event()
        seen: dict[str, str] = {}

        async def handler(ws: ServerConnection) -> None:
            seen.update({k.lower(): v for k, v in ws.request.headers.items()})
            await ws.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = FakeClientWebSocket()
            client.headers = Headers(client_headers)
            await proxy_websocket_request(
                cast(WebSocket[Any, Any, Any], client), port, strip_authorization=strip_authorization
            )
            return seen

    return asyncio.run(run())


def test_ws_strips_authorization_when_requested() -> None:
    seen = _headers_backend_sees({"Authorization": "Bearer owner-api-token"}, strip_authorization=True)
    assert "authorization" not in seen


def test_ws_forwards_app_owned_authorization_by_default() -> None:
    seen = _headers_backend_sees({"Authorization": "Bearer app-own-jwt"}, strip_authorization=False)
    assert seen.get("authorization") == "Bearer app-own-jwt"
