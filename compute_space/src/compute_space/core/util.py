from __future__ import annotations

import asyncio
import functools
import ipaddress
import os
import socket
from collections.abc import Callable
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import attr


def default_route_source_ip() -> str | None:
    """The address a peer reaches us at, or None. Prefers the default-route egress interface, then
    falls back to a private/link-local LAN address when there is no default route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = str(sock.getsockname()[0])
        if not _is_loopback_or_unspecified(ip):
            return ip
    except OSError:
        pass
    return _lan_ip_from_host()


def _is_loopback_or_unspecified(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_loopback or addr.is_unspecified


def _lan_ip_from_host() -> str | None:
    """Best private (then link-local) IPv4 among the host's resolved addresses, or None."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
    except OSError:
        return None
    link_local: str | None = None
    for info in infos:
        ip = str(info[4][0])
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_unspecified:
            continue
        if addr.is_private and not addr.is_link_local:
            return ip
        if addr.is_link_local and link_local is None:
            link_local = ip
    return link_local


def write_restricted(path: Path, content: str) -> None:
    """Write a file that is only readable by the owner (mode 0o600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def assert_type[T](value: Any, expected_type: type[T]) -> T:
    if not isinstance(value, expected_type):
        raise TypeError(f"expected type {expected_type.__name__}, got {type(value).__name__}")
    return value


def assert_str(value: Any) -> str:
    return assert_type(value, str)


def assert_int(value: Any) -> int:
    return assert_type(value, int)


def async_wrap[T, **P](func: Callable[P, T]) -> Callable[P, Coroutine[Any, Any, T]]:
    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


def not_blank(instance: object, attribute: attr.Attribute[str], value: str) -> None:
    if not value.strip():
        raise ValueError(f"{attribute.name} must not be blank")
