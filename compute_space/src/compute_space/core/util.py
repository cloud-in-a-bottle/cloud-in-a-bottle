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

_PROBE_TARGET = {
    socket.AF_INET: ("8.8.8.8", 80),
    socket.AF_INET6: ("2001:4860:4860::8888", 80),
}


def default_route_source_ip(family: socket.AddressFamily = socket.AF_INET) -> str | None:
    """The address a peer reaches us at over ``family``, or None. Prefers the default-route egress
    interface, then falls back to a LAN address when there is no default route."""
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect(_PROBE_TARGET[family])
            ip = _strip_scope(str(sock.getsockname()[0]))
        if _is_publishable(ip, family):
            return ip
    except OSError:
        pass
    return _lan_ip_from_host(family)


def _strip_scope(ip: str) -> str:
    """Drop an IPv6 ``%interface`` suffix — it is meaningful only to the local host."""
    return ip.split("%")[0]


def _is_publishable(ip: str, family: socket.AddressFamily) -> bool:
    """Usable as an address we hand to clients: not loopback/unspecified, and for IPv6 not
    link-local — ``fe80::`` needs a ``%interface`` scope no URL or DNS record can carry."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_unspecified:
        return False
    return not (family == socket.AF_INET6 and addr.is_link_local)


def _lan_ip_from_host(family: socket.AddressFamily) -> str | None:
    """Best LAN address among the host's own resolved addresses, or None.  IPv4 prefers a private
    address and falls back to link-local; IPv6 takes any publishable one (global or ULA)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, family=family)
    except OSError:
        return None
    link_local: str | None = None
    for info in infos:
        ip = _strip_scope(str(info[4][0]))
        if not _is_publishable(ip, family):
            continue
        if family == socket.AF_INET6:
            return ip  # link-local already excluded by _is_publishable
        addr = ipaddress.ip_address(ip)
        if addr.is_private and not addr.is_link_local:
            return ip
        if addr.is_link_local and link_local is None:
            link_local = ip
    return link_local


def is_reachable(ip: str, port: int, timeout: float = 0.5) -> bool:
    """True if a TCP connect to ``ip``:``port`` succeeds.

    Clients prefer IPv6 (RFC 6724), so publishing an address nothing listens on turns every
    connection into a timeout-then-fallback — strictly worse than publishing no AAAA at all.
    """
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
        return True
    except OSError:
        return False


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
