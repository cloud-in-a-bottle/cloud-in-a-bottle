"""Shared setup for DNS tests: a seeded instance whose zone files really exist on disk."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import cast

from litestar import Litestar

from compute_space.config import DefaultConfig
from compute_space.config import set_active_config
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.coredns_provider.interface import ADDRESS_TTL_SECONDS
from compute_space.core.dns.coredns_provider.interface import DnsZone
from compute_space.core.dns.coredns_provider.interface import build_coredns_service_app
from compute_space.core.dns.coredns_provider.interface import public_dns_zones
from compute_space.core.dns.coredns_provider.interface import write_coredns_config
from compute_space.core.dns.router_records import ROUTER_ADDRESS_NAMES
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import DNS_SERVICE_VERSION
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import RecordType
from compute_space.core.dns.settings import dns_settings_for
from compute_space.core.dns.settings import zones_for_domains
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.core.proxy_target import AsgiApp
from compute_space.core.service_interface.builtin_services import BuiltinService
from compute_space.core.service_interface.builtin_services import register_builtin_service
from compute_space.db import init_db
from compute_space.db import provide_db
from compute_space.tests.conftest import open_db

PUBLIC_IP = "203.0.113.10"


def dns_service_app_for(config: DefaultConfig) -> Litestar:
    """The service API over ``config``'s DB, with the zone set re-derived per request."""
    settings = dns_settings_for(config, PUBLIC_IP, container_gateway_ip=None)

    def zones() -> tuple[DnsZone, ...]:
        with closing(open_db(config)) as db:
            return public_dns_zones(settings, zones_for_domains(db))

    return build_coredns_service_app(provide_db, settings, zones)


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
    set_active_config(config)
    # start.py registers the builtin at boot; a test that resolves the `dns` service needs the
    # same thing done, or the router has no provider for it.
    register_builtin_service(
        BuiltinService(
            service_url=DNS_SERVICE_URL,
            version=DNS_SERVICE_VERSION,
            app=cast(AsgiApp, dns_service_app_for(config)),
        )
    )
    with closing(open_db(config)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
        settings = dns_settings_for(config, public_ip, container_gateway_ip=None)
        # The records that route the space: start.py publishes these over the service API once
        # CoreDNS is up, so a test wanting a realistic zone file needs them too.
        store.set_records(
            db, [DnsRecord(n, RecordType.A, ADDRESS_TTL_SECONDS, public_ip) for n in ROUTER_ADDRESS_NAMES]
        )
        write_coredns_config(public_dns_zones(settings, zones_for_domains(db)), settings, db=db)
    return config
