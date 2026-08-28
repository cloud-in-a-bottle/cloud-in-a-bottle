"""Where the instance thinks it is.

The DB is the source of truth; ``public_ip`` in config.toml only seeds it, so a dynamic update
isn't undone by a stale config file on the next restart.

Detection is paranoid on purpose: a wrong answer rewrites the apex and wildcard A records and
takes the whole space offline, and a NAT or captive portal answering an echo service is exactly
how that happens.
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

# Independent operators, so one being wrong or hijacked can't carry a majority.
_ECHO_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://ipv4.icanhazip.com",
)
_REQUIRED_AGREEMENT = 2
_TIMEOUT_SECONDS = 10.0


def effective_public_ip(config: Config, db: sqlite3.Connection) -> str | None:
    return get_setting(db, PUBLIC_IP_KEY) or config.public_ip


def seed_public_ip(config: Config, db: sqlite3.Connection) -> None:
    """Copy the config value into the DB once.  Never overwrites: after first boot the DB is
    authoritative, and a config file written before the machine moved must not win."""
    if config.public_ip and get_setting(db, PUBLIC_IP_KEY) is None:
        set_setting(db, PUBLIC_IP_KEY, config.public_ip)
        logger.info(f"Seeded public IP {config.public_ip} from config")


def store_public_ip(db: sqlite3.Connection, ip: str) -> None:
    set_setting(db, PUBLIC_IP_KEY, ip)


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
    """The address at least ``required_agreement`` sources agree on, or None.

    None means "leave the records alone", not "the IP went away".
    """
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

    logger.warning(f"No agreed public IP ({votes or 'no source reachable'}); leaving the current value in place")
    return None
