"""Where the instance thinks it is.

The DB is the source of truth; ``public_ip`` in config.toml only seeds it, so a later update
isn't undone by a stale config file on the next restart.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

PUBLIC_IP_KEY = "public_ip"


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
