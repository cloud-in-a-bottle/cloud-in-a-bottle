"""The instance's public IP: where it's stored, and how it's re-detected at runtime.

Historically this was a static ``public_ip`` in config.toml, which meant a machine whose address
moved — a home server on a residential connection, or a VPS that got renumbered — needed a config
edit and a restart to come back.  The DB is now the source of truth and the config value only
seeds it, so the dynamic-DNS watcher can update it in place.

Detection is deliberately paranoid.  A wrong answer here rewrites the apex and wildcard A records
and takes the whole space offline, and the failure mode of a single flaky echo service returning
a proxy's address is exactly that.  So a value is only accepted when independent sources agree.
"""

from __future__ import annotations

import ipaddress
import sqlite3

import httpx

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

PUBLIC_IP_KEY = "public_ip"

# Independent operators, so one of them being wrong or hijacked doesn't carry a majority.
_ECHO_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://ipv4.icanhazip.com",
)

# How many sources must return the same address before it is believed.
_REQUIRED_AGREEMENT = 2

_TIMEOUT_SECONDS = 10.0


def effective_public_ip(config: Config, db: sqlite3.Connection) -> str | None:
    """The instance's current public IP: the stored value, falling back to the config seed."""
    return get_setting(db, PUBLIC_IP_KEY) or config.public_ip


def seed_public_ip(config: Config, db: sqlite3.Connection) -> None:
    """Copy the config's public IP into the DB once, so later updates have somewhere to land.

    Never overwrites: after first boot the DB is authoritative, and a stale config.toml on a
    machine that has since moved must not be able to undo a dynamic update.
    """
    if config.public_ip and get_setting(db, PUBLIC_IP_KEY) is None:
        set_setting(db, PUBLIC_IP_KEY, config.public_ip)
        logger.info(f"Seeded public IP {config.public_ip} from config")


def store_public_ip(db: sqlite3.Connection, ip: str) -> None:
    set_setting(db, PUBLIC_IP_KEY, ip)


def is_public_ipv4(candidate: str) -> bool:
    """True for an IPv4 address that is actually reachable from the internet.

    ``is_global`` is exactly the question being asked — it consults the IANA special-purpose
    registry, so RFC 1918, loopback, link-local, CGNAT, and the documentation ranges are all
    rejected in one check.  Publishing any of them as the space's A record makes it unreachable,
    and a NAT or captive portal answering the echo service is precisely how that happens.
    """
    try:
        return ipaddress.IPv4Address(candidate.strip()).is_global
    except ValueError:
        return False


def detect_public_ip(
    services: tuple[str, ...] = _ECHO_SERVICES,
    required_agreement: int = _REQUIRED_AGREEMENT,
) -> str | None:
    """Ask several echo services and return the address at least ``required_agreement`` agree on.

    None when they can't be reached or don't agree — which the caller must treat as "leave the
    records alone", not as "the IP went away".
    """
    votes: dict[str, int] = {}
    with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
        for url in services:
            try:
                answer = client.get(url).text.strip()
            except httpx.HTTPError as e:
                logger.debug(f"Public IP source {url} failed: {e}")
                continue
            if not is_public_ipv4(answer):
                logger.debug(f"Public IP source {url} returned a non-public address {answer!r}")
                continue
            votes[answer] = votes.get(answer, 0) + 1
            if votes[answer] >= required_agreement:
                return answer

    if votes:
        logger.warning(f"Public IP sources disagreed ({votes}); leaving the current value in place")
    else:
        logger.warning("No public IP source could be reached; leaving the current value in place")
    return None
