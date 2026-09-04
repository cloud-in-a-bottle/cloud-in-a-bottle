from __future__ import annotations

import ipaddress
import socket

import httpx

from compute_space.core.logging import logger

_ECHO_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://ipv4.icanhazip.com",
)
_REQUIRED_AGREEMENT = 2
_TIMEOUT_SECONDS = 10.0


def _strip_scope(ip: str) -> str:
    """Drop an IPv6 ``%interface`` suffix — it is meaningful only to the local host."""
    return ip.split("%")[0]


def is_public_ipv4(candidate: str) -> bool:
    """``is_global`` consults the IANA special-purpose registry, so RFC 1918, loopback,
    link-local, CGNAT, and the documentation ranges are all rejected in one check."""
    try:
        return ipaddress.IPv4Address(candidate.strip()).is_global
    except ValueError:
        return False


async def detect_public_ip(
    services: tuple[str, ...] = _ECHO_SERVICES,
    required_agreement: int = _REQUIRED_AGREEMENT,
) -> str | None:
    """The address at least ``required_agreement`` sources agree on, or None"""
    votes: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
        for url in services:
            try:
                answer = (await client.get(url)).text.strip()
            except httpx.HTTPError as e:
                logger.debug(f"Public IP source {url} failed: {e}")
                continue
            if not is_public_ipv4(answer):
                logger.debug(f"Public IP source {url} returned a non-public address {answer!r}")
                continue
            votes[answer] = votes.get(answer, 0) + 1
            if votes[answer] >= required_agreement:
                return answer
    return None


def source_ip_for(dest: str) -> str | None:
    """The local address the kernel would send to ``dest`` from, or None if it has no route."""
    family = socket.AF_INET6 if ":" in dest else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect((dest, 9))  # discard port; nothing is sent to it
            return _strip_scope(str(sock.getsockname()[0]))
    except OSError:
        return None


def default_outbound_interface_ipv4() -> str | None:
    """The local IPv4 that internet-bound traffic leaves from, or None if it cannot be determined."""
    return source_ip_for("8.8.8.8")


def is_bindable(ip: str) -> bool:
    """True if ``ip`` is an address on one of this box's interfaces, probed by binding an ephemeral
    UDP port.  A NATed box's public IP and a not-yet-created dummy interface both fail this."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((ip, 0))
        return True
    except OSError:
        return False


def infer_inbound_ipv4(public_ip: str) -> str | None:
    # first see if we can directly bind the public ip; if so that means we're not behind NAT and public=private.
    if is_bindable(public_ip):
        return public_ip
    # otherwise the best we can do is see what interface we use for outbound traffic,
    # and assume inbound comes the same way.
    return default_outbound_interface_ipv4()
