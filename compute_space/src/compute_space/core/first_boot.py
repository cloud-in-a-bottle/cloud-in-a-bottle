"""First-boot seeding.

A ``first_boot.toml`` next to the router config supplies a fresh instance's primary domain +
claim token; they are seeded into the DB (the ``domains`` table + the ``settings`` claim token)
exactly once.  On an old instance there is no ``first_boot.toml`` — the domain set is seeded from the
config-file ``zone_domain`` + ``[[openhost.domains]]`` and the claim token from its legacy file.
After first boot the DB is authoritative and these file sources are never read again.
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

import attr

from compute_space.config import Config
from compute_space.config import Domain
from compute_space.core import domain_store
from compute_space.core import settings_store
from compute_space.core import system_agent
from compute_space.core.logging import logger


@attr.s(auto_attribs=True, frozen=True)
class FirstBoot:
    domain: str
    claim_token: str | None
    tls: bool
    mdns: bool


def _config_dir() -> Path | None:
    """Directory of the router config file (``first_boot.toml`` lives beside it)."""
    path = os.environ.get("OPENHOST_ROUTER_CONFIG") or os.environ.get("OPENHOST_CONFIG")
    return Path(path).parent if path else None


def read_first_boot() -> FirstBoot | None:
    """Parse ``first_boot.toml`` from the router-config directory, or None if there's no config-file
    path or the file is absent.  A missing/blank ``domain`` is ignored (treated as no first-boot)."""
    config_dir = _config_dir()
    if config_dir is None:
        return None
    fb_path = config_dir / "first_boot.toml"
    if not fb_path.exists():
        return None
    with open(fb_path, "rb") as f:
        data = tomllib.load(f)
    domain = str(data.get("domain", "")).strip()
    if not domain:
        logger.warning("{} has no `domain`; ignoring", fb_path)
        return None
    return FirstBoot(
        domain=domain,
        claim_token=(str(data["claim_token"]) if data.get("claim_token") else None),
        tls=bool(data.get("tls", True)),
        mdns=bool(data.get("mdns", False)),
    )


def _owner_exists(config: Config) -> bool:
    """True once /setup has provisioned the owner — after which the claim token is moot."""
    try:
        with closing(sqlite3.connect(config.db_path)) as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _read_legacy_claim_token(config: Config) -> str | None:
    """The pre-DB claim token from its standalone file (upgrade path).  The file may hold
    ``token:extra``; the token is the part before the first colon."""
    try:
        content = Path(config.claim_token_path).read_text().strip()
    except OSError:
        return None
    return content.split(":", 1)[0] or None


def _scrub_zone_domain_line() -> None:
    """Remove the now-captured ``zone_domain`` line from ``config.toml`` — delegated to the system
    agent so all config-file mutation goes through the single privileged writer (the agent runs as
    root and preserves the file's ``host:host`` ownership).  Best-effort: the value is already in the
    DB and is never read at runtime, so a failure (e.g. no agent in dev/CI) must not block startup.
    ``config.toml`` still loads afterwards because ``zone_domain`` is optional in ``DefaultConfig``."""
    try:
        system_agent.scrub_config_zone_domain()
        logger.info("Requested config.toml zone_domain scrub via the system agent")
    except system_agent.SystemAgentError as exc:
        logger.warning("Could not scrub zone_domain via the system agent ({}); it is ignored at runtime anyway", exc)


def seed_first_boot(config: Config) -> None:
    """Seed the DB (domains + claim token) once, on first boot.  Idempotent — no-op once the
    ``domains`` table has rows.  Prefers ``first_boot.toml``; otherwise the config-file ``zone_domain``
    + ``[[openhost.domains]]`` (domains) and the legacy claim-token file (token)."""
    fb = read_first_boot()
    if fb is not None:
        primary = Domain(name=fb.domain, tls=fb.tls, mdns=fb.mdns)
        seeded = domain_store.seed_domains(config, primary, [])
    else:
        seeded = domain_store.seed_domains_from_legacy(config)

    # Migrate the claim token into the settings store.  Relevant only pre-setup, so gate on
    # owner-absence + settings-absence rather than the domain-seeded flag — an instance that seeded
    # its domains under an earlier build still gets its token migrated before /setup runs.
    if not _owner_exists(config) and settings_store.get_setting(config, settings_store.CLAIM_TOKEN_KEY) is None:
        token = (fb.claim_token if fb is not None else None) or _read_legacy_claim_token(config)
        if token:
            settings_store.set_setting(config, settings_store.CLAIM_TOKEN_KEY, token)
            logger.info("Seeded claim token into the settings store")

    # Scrub the captured zone_domain line from config.toml only when we migrated the domain set this
    # boot (i.e. the DB was empty); afterwards the DB is authoritative and the line is never read.
    if seeded:
        _scrub_zone_domain_line()
