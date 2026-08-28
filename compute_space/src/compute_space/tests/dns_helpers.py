"""Shared setup for DNS tests: a seeded instance whose zone files really exist on disk."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from compute_space.config import DefaultConfig
from compute_space.config import set_active_config
from compute_space.core.dns.coredns_provider.coredns import _write_coredns_config
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.db import init_db
from compute_space.tests.conftest import open_db

PUBLIC_IP = "203.0.113.10"


def seeded_dns_config(tmp_path: Path, *domains: Domain, public_ip: str = PUBLIC_IP) -> DefaultConfig:
    """A config with a live DB and seeded CoreDNS zone files for ``domains`` (primary first).

    Real zone files rather than stubs, so tests exercise the same read/write path production does.
    """
    config = DefaultConfig(
        data_root_dir=str(tmp_path),
        public_ip=public_ip,
        coredns_enabled=True,
    )
    config.make_all_dirs()
    init_db(config.db_path)
    # The provider's routes read the active config, as every other route does.
    set_active_config(config)
    with closing(open_db(config)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
        _write_coredns_config(public_dns_zones(config, db), public_ip, config.coredns_corefile_path, None, db=db)
    return config
