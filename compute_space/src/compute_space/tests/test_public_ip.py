"""Where the instance thinks it is: which of the config value and the stored value wins."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from compute_space.config import DefaultConfig
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import seed_public_ip
from compute_space.core.dns.public_ip import store_public_ip
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.db import init_db
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config

ZONE = "host.example.com"


def _space(tmp_path: Path, public_ip: str | None = "203.0.113.10") -> DefaultConfig:
    if public_ip is None:
        # No IP means no zone files to seed; the test only cares about storage precedence.
        config = DefaultConfig(data_root_dir=str(tmp_path), public_ip=None)
        config.make_all_dirs()
        init_db(config.db_path)
        with closing(open_db(config)) as db:
            seed_domains(db, Domain(ZONE, tls=True), [])
        return config
    return seeded_dns_config(tmp_path, Domain(ZONE, tls=True), public_ip=public_ip)


def test_the_config_value_seeds_the_db_once(tmp_path: Path) -> None:
    config = _space(tmp_path)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert effective_public_ip(config, db) == "203.0.113.10"


def test_a_stale_config_cannot_undo_a_stored_update(tmp_path: Path) -> None:
    # The machine moved and the DB knows; a config.toml that was written before the move must not
    # win on the next restart.
    config = _space(tmp_path)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        store_public_ip(db, "198.51.100.7")
        seed_public_ip(config, db)  # as a restart would
        assert effective_public_ip(config, db) == "198.51.100.7"


def test_with_no_stored_or_configured_ip_there_is_none(tmp_path: Path) -> None:
    config = _space(tmp_path, public_ip=None)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert effective_public_ip(config, db) is None
