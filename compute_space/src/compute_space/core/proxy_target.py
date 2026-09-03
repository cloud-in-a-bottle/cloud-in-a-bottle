from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from typing import Any
from typing import cast

import attr
import httpx

# The ASGI callable, spelled out here so core doesn't depend on a web framework's alias for it.
# An ASGI scope really is a str-keyed mapping of arbitrary values, so the Any is honest.
AsgiScope = MutableMapping[str, Any]
AsgiReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
AsgiSend = Callable[[MutableMapping[str, Any]], Awaitable[None]]
AsgiApp = Callable[[AsgiScope, AsgiReceive, AsgiSend], Awaitable[None]]


@attr.s(auto_attribs=True, frozen=True)
class LocalPort:
    """Something listening on loopback."""

    port: int


@attr.s(auto_attribs=True, frozen=True)
class InProcess:
    """An ASGI app we serve ourselves, reached with no socket in between."""

    app: AsgiApp


# Where a proxied request should be sent.  Callers switch on this and nothing else, which is what
# lets an in-process implementation and a real app be held to the same contract.
ProxyTarget = LocalPort | InProcess


# Host for in-process calls.  Never resolved — the transport answers before any lookup — but httpx
# needs a valid absolute URL.
BUILTIN_HOST = "http://builtin.openhost.internal"


def client_for(target: ProxyTarget, timeout: httpx.Timeout | float) -> tuple[httpx.AsyncClient, str]:
    """An httpx client and base URL for a target, in-process or over loopback."""
    match target:
        case InProcess(app):
            # cast: litestar types its ASGIApp with its own scope classes, httpx with the raw
            # MutableMappings; they are the same protocol.
            transport: httpx.AsyncBaseTransport | None = httpx.ASGITransport(app=cast(Any, app))
            base_url = BUILTIN_HOST
        case LocalPort(port):
            transport, base_url = None, f"http://127.0.0.1:{port}"
    return httpx.AsyncClient(transport=transport, timeout=timeout), base_url
