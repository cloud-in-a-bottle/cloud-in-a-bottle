"""First-boot seeding.

A ``first_boot.toml`` next to the router config supplies a fresh instance's primary domain +
claim token; they are seeded into the DB (the ``domains`` table + the ``settings`` claim token)
exactly once, after which the DB is authoritative.  Old instances (whose domain lived in
``config.toml``) are migrated into the DB out of band by the system-agent migration
``v0007_seed_domains_and_scrub``, which also scrubs the captured ``config.toml`` lines.
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

import attr

from compute_space.config import Config
from compute_space.core import settings_store
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.core.logging import logger
from compute_space.db import get_db


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


def owner_exists(config: Config) -> bool:
    """True once /setup has provisioned the owner — after which the claim token is moot.
    Fails loud: the schema is initialized before first-boot seeding, so a DB error is real."""
    with closing(sqlite3.connect(config.db_path)) as db:
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def _read_legacy_claim_token(config: Config) -> str | None:
    """The pre-DB claim token from its standalone file (upgrade path).  The file may hold
    ``token:extra``; the token is the part before the first colon."""
    try:
        content = Path(config.claim_token_path).read_text().strip()
    except OSError:
        return None
    return content.split(":", 1)[0] or None


def seed_first_boot(config: Config) -> None:
    """Seed the DB (domains + claim token) once, on first boot from ``first_boot.toml`` (falling back
    to the legacy claim-token file for the token).  Idempotent — no-op once the ``domains`` table has
    rows.  Old-instance config.toml capture is handled by system-agent ``v0007_seed_domains_and_scrub``."""
    fb = read_first_boot()
    with closing(get_db()) as db:
        if fb is not None:
            primary = Domain(name=fb.domain, tls=fb.tls, mdns=fb.mdns)
            seed_domains(db, primary, [])

        # Migrate the claim token into the settings store.  Relevant only pre-setup, so gate on
        # owner-absence + settings-absence rather than the domain-seeded flag — an instance that seeded
        # its domains under an earlier build still gets its token migrated before /setup runs.
        if not owner_exists(config) and settings_store.get_setting(db, settings_store.CLAIM_TOKEN_KEY) is None:
            token = (fb.claim_token if fb is not None else None) or _read_legacy_claim_token(config)
            if token:
                settings_store.set_setting(db, settings_store.CLAIM_TOKEN_KEY, token)
                logger.info("Seeded claim token into the settings store")
