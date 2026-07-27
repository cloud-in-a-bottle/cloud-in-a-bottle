"""First-boot seeding.

A ``first_boot.toml`` next to the router config supplies a fresh instance's primary domain +
claim token; they are seeded into the DB (the ``domains`` table + the ``settings`` claim token)
exactly once.  On an old instance there is no ``first_boot.toml`` — the domain set is seeded from the
config-file ``zone_domain`` + ``[[openhost.domains]]`` and the claim token from its legacy file.
After first boot the DB is authoritative and these file sources aren't read again; the old-instance
system-agent migration ``v0007_seed_domains_and_scrub`` scrubs the captured ``zone_domain`` line from
``config.toml`` (for a fresh install, on its first update).
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


def seed_first_boot(config: Config) -> None:
    """Seed the DB (domains + claim token) once, on first boot.  Idempotent — no-op once the
    ``domains`` table has rows.  Prefers ``first_boot.toml``; otherwise the config-file ``zone_domain``
    + ``[[openhost.domains]]`` (domains) and the legacy claim-token file (token).

    This only *captures* into the DB; it never edits ``config.toml``.  Removing the captured
    ``zone_domain`` line is the old-instance system-agent migration ``v0007_seed_domains_and_scrub``
    (the runtime seed can't scrub without racing its own capture)."""
    fb = read_first_boot()
    if fb is not None:
        primary = Domain(name=fb.domain, tls=fb.tls, mdns=fb.mdns)
        domain_store.seed_domains(config, primary, [])
    else:
        domain_store.seed_domains_from_legacy(config)

    # Migrate the claim token into the settings store.  Relevant only pre-setup, so gate on
    # owner-absence + settings-absence rather than the domain-seeded flag — an instance that seeded
    # its domains under an earlier build still gets its token migrated before /setup runs.
    if not _owner_exists(config) and settings_store.get_setting(config, settings_store.CLAIM_TOKEN_KEY) is None:
        token = (fb.claim_token if fb is not None else None) or _read_legacy_claim_token(config)
        if token:
            settings_store.set_setting(config, settings_store.CLAIM_TOKEN_KEY, token)
            logger.info("Seeded claim token into the settings store")
