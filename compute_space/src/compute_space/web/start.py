import asyncio
import contextlib
import os
import shutil
import signal
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import hypercorn.asyncio
import hypercorn.config

from compute_space.config import Config
from compute_space.config import load_config
from compute_space.config import set_active_config
from compute_space.core.auth.keys import load_keys
from compute_space.core.caddy import CaddyProcess
from compute_space.core.caddy import config_cert_resolver
from compute_space.core.caddy import reload_caddy_for_domains
from compute_space.core.caddy import set_active_caddy
from compute_space.core.caddy import start_caddy
from compute_space.core.caddy import unix_admin_address
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.dns.coredns_provider.interface import DnsNotEnabled
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.router_records import publish_router_addresses
from compute_space.core.domains import Domain
from compute_space.core.domains import effective_domains
from compute_space.core.domains import primary_domain
from compute_space.core.first_boot import owner_exists
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.ip import infer_inbound_ipv4
from compute_space.core.ip import is_bindable
from compute_space.core.logging import logger
from compute_space.core.logging import setup_file_logging
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.core.pinned_binary import install_pinned_binary
from compute_space.core.system_agent.client import system_agent_stop_updater_sync
from compute_space.core.system_agent.progress import mark_boot_complete
from compute_space.core.terminal import cleanup_all as cleanup_terminal_sessions
from compute_space.core.tls.provision import provision_cert
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status
from compute_space.core.tls.renewal import start_renewal_task
from compute_space.core.updates import RESTART_EXIT_CODE
from compute_space.core.updates import initialize_shutdown_event
from compute_space.db import get_db
from compute_space.db import init_db
from compute_space.web.app import create_app
from compute_space.web.setup_app import create_setup_app
from openhost_system_agent.updater.paths import DATA_DIR_ENV


def _bootstrap(config: Config) -> None:
    """One-time process-wide initialization shared by the setup and full apps."""
    set_active_config(config)
    # Process-wide rather than an argument to the agent calls, because we resolve
    # these paths in-process too: compute_space reads the progress log and appends
    # to it (read_progress, mark_boot_complete, record_apply_failure) through the
    # same shared module the agent, the apply unit and the updater use, and that
    # module resolves the directory from this variable so all four agree on one
    # path. Agent invocations forward it explicitly on top (see _agent_argv).
    os.environ[DATA_DIR_ENV] = str(config.openhost_data_path)
    setup_file_logging(Path(os.path.dirname(config.db_path)) / "compute_space.log")
    load_keys(config.keys_dir)
    init_db(config.db_path)


def _require_configured_domain(domains: tuple[Domain, ...]) -> None:
    """Fail loud at boot if nothing seeded the DB `domains` table."""
    if not domains:
        raise RuntimeError(
            "No domain configured: nothing seeded the DB `domains` table. "
            "Set a domain in first_boot.toml, then restart."
        )


async def _ensure_tls_cert(config: Config, db: sqlite3.Connection, dns_provider: InternalDnsProvider) -> None:
    """Make sure a usable cert+key pair is on disk before Caddy starts, acquiring or renewing as configured."""
    primary = primary_domain(db)
    if not primary.tls:
        return
    cert_path, key_path = config.cert_key_paths_for(db, primary.name_no_port)
    status = get_cert_status(cert_path, key_path)
    if status == CertStatus.OK:
        logger.info(f"Using existing TLS cert from {cert_path}")
        return
    if not config.coredns_enabled or not config.acquire_tls_cert_if_missing:
        # A cert nearing expiry still works, so don't block startup over it.
        if status == CertStatus.EXPIRING_SOON:
            logger.warning("TLS cert expires soon but automatic cert acquisition is not enabled; cannot renew")
            return
        if not config.coredns_enabled:
            raise RuntimeError("CoreDNS must be enabled to acquire TLS cert via DNS-01 challenge")
        raise RuntimeError(f"TLS cert is {status.value} and acquire_tls_cert_if_missing is False")
    if status == CertStatus.EXPIRING_SOON:
        # The existing cert is still valid, so a failed renewal shouldn't block
        # startup — the background renewal loop will keep retrying.
        try:
            await provision_cert(config, db, dns_provider)
        except Exception:
            logger.exception("TLS cert renewal failed; serving the existing cert and retrying in the background")
    else:
        await provision_cert(config, db, dns_provider)


def _hairpin_gateway_ip() -> str | None:
    if not is_bindable(CONTAINER_GATEWAY_IP):
        logger.info(f"Container gateway {CONTAINER_GATEWAY_IP} not bindable; serving no container-facing DNS view")
        return None
    return CONTAINER_GATEWAY_IP


def _dns_bind_ip(config: Config) -> str:
    """The local address CoreDNS serves the public zones on."""
    if not config.public_ip:
        raise RuntimeError("Public IP must be set in config to use CoreDNS")
    bind_ip = infer_inbound_ipv4(config.public_ip)
    if bind_ip is None:
        raise RuntimeError("CoreDNS is enabled but no local address to bind to could be inferred")
    return bind_ip


async def _start_dns(config: Config, domains: tuple[Domain, ...]) -> InternalDnsProvider:
    """Build the DNS provider and bring up the zones for ``domains``."""
    dns_provider = InternalDnsProvider(
        corefile_path=config.coredns_corefile_path,
        zones_dir=config.zones_dir,
        # None disables serving DNS entirely for the main routing records
        bind_ip=_dns_bind_ip(config) if config.coredns_enabled else None,
        container_gateway_ip=_hairpin_gateway_ip(),
        coredns_bin=_ensure_coredns_binary(config) if config.coredns_enabled else "coredns",
    )

    # Before the first add_zone, so the zones CoreDNS starts on already carry the A records routing
    # the space.  Starting without them serves NODATA at the apex and NXDOMAIN for every
    # `<app>.<domain>` until the next reload, and resolvers negative-cache both.
    if config.public_ip is not None:
        publish_router_addresses(dns_provider, config.public_ip)

    for domain in domains:
        try:
            # slightly inefficient bc we reboot coredns each time, but simpler than a separate batch path
            await dns_provider.add_zone(domain.name_no_port)
        except DnsNotEnabled:
            # Running without CoreDNS is a supported choice; the domain is still served by Caddy,
            # and a TLS one reports the consequence through its cert status.
            logger.warning("Not serving DNS for {}: coredns is not enabled", domain.name_no_port)

    return dns_provider


def _ensure_coredns_binary(config: Config) -> str:
    """Return the CoreDNS binary to launch, self-healing a missing one."""
    if found := shutil.which("coredns"):
        return found
    dest = str(config.openhost_data_path / "coredns")
    install_pinned_binary(get_pinned_binary("coredns"), dest)
    return dest


def main() -> None:
    asyncio.run(_main())


async def _main() -> None:
    # Allow group members to write files/dirs we create (files 664, dirs 775).
    os.umask(0o002)

    config = load_config()
    config.make_all_dirs()
    _bootstrap(config)
    # The DB `domains` table is the source of truth.  Seed it once (+ the claim token) from
    # first_boot.toml before starting CoreDNS/Caddy so every configured domain is served this boot.
    seed_first_boot(config)
    caddy: CaddyProcess | None = None
    # Long-running tasks started at boot.  Held here for their whole lifetime (the loop keeps only
    # weak references) and cancelled together by _shutdown.
    background_tasks: list[asyncio.Task[None]] = []
    # One connection for the whole domain-dependent startup sequence (CoreDNS -> TLS cert -> Caddy);
    # the primary + cert/zone paths are read live from it.
    with closing(get_db()) as db:
        domains = effective_domains(db)
        _require_configured_domain(domains)

        dns_provider = await _start_dns(config, domains)

        if domains[0].tls:  # primary is a TLS domain
            await _ensure_tls_cert(config, db, dns_provider)

        # Caddy reverse proxy. mainly for TLS termination, but also some other features.
        # The acquired file cert covers the primary domain (a wildcard for it);
        # any additional TLS domains fall back to Caddy's internal CA (see generate_caddyfile).
        needs_caddy_for_tls = any(d.tls for d in domains)
        if config.start_caddy:
            # Release 80/443 from the detached updater (if a self-update just
            # happened) before Caddy binds, so Caddy can't lose the handoff race.
            await asyncio.to_thread(system_agent_stop_updater_sync)
            caddy = await start_caddy(
                config.caddyfile_path,
                domains,
                config.port,
                cert_for=config_cert_resolver(config, db),
                admin_addr=unix_admin_address(config.caddy_admin_socket_path),
            )
            # Register so /api/domains can regenerate + restart Caddy when a domain is added/removed.
            set_active_caddy(caddy)
            if needs_caddy_for_tls and config.coredns_enabled and config.acquire_tls_cert_if_missing:
                # Renew every TLS domain — including a TLS secondary under a non-TLS primary — and
                # regenerate the Caddyfile so acquired certs are served.
                background_tasks.append(start_renewal_task(reload_caddy_for_domains, dns_provider))
        elif needs_caddy_for_tls:
            raise RuntimeError(
                "A TLS domain is configured but start_caddy is False. Caddy is required for TLS termination."
            )

    # Finalize the progress log only now that we're actually serving, so the
    # /updating page's "back online" doesn't fire before CoreDNS/cert/Caddy are up.
    mark_boot_complete()

    async def _shutdown() -> None:
        """Stop the background tasks and the child processes.  Each handle stops its own process and
        log task, which restart() may have replaced since startup."""
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if caddy is not None:
            await caddy.stop()
        await dns_provider.cleanup()

    hypercorn_config = hypercorn.config.Config()
    # Bind the primary address (127.0.0.1 in production) plus the container
    # gateway (10.200.0.1) so podman containers can reach the router via
    # host.containers.internal.  No need for 0.0.0.0 — Caddy handles
    # external traffic on 80/443 and proxies to us on loopback.
    binds = [f"{config.host}:{config.port}"]
    container_gateway = "10.200.0.1"
    if config.host != "0.0.0.0" and config.host != container_gateway:
        # Only add the gateway bind if the interface actually exists (it won't
        # in dev mode or CI where openhost0 hasn't been created by ansible).
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind((container_gateway, 0))
            probe.close()
            binds.append(f"{container_gateway}:{config.port}")
        except OSError:
            pass
    hypercorn_config.bind = binds
    hypercorn_config.graceful_timeout = 3
    hypercorn_config.shutdown_timeout = 5

    # First-boot setup: serve a minimal app until the owner is provisioned.  The setup
    # handler triggers shutdown via trigger_restart(); we then proceed to the full app
    if not owner_exists(config):
        logger.info("No owner row found; serving setup-only app")
        setup_completed = await _serve(create_setup_app(config), hypercorn_config)
        if not setup_completed:
            logger.info("Setup interrupted by signal; exiting")
            await _shutdown()
            await asyncio.sleep(0.1)
            os._exit(0)

    # Main web server
    app = create_app(config, dns_provider)
    logger.info("running hypercorn serve")
    restart_requested = await _serve(app, hypercorn_config)
    logger.info(f"hypercorn serve returned, restart_requested={restart_requested}")

    await _shutdown()

    if restart_requested:
        logger.info(f"Calling os._exit({RESTART_EXIT_CODE})")
        await asyncio.sleep(0.1)
        os._exit(RESTART_EXIT_CODE)

    logger.info("Calling os._exit(0)")
    await asyncio.sleep(0.1)
    os._exit(0)


async def _serve(app: Any, hypercorn_config: hypercorn.config.Config) -> bool:
    """Run hypercorn with a shutdown trigger wired to the update system.

    Returns True if shutdown was triggered by a restart request (not a signal).
    """
    shutdown_event = asyncio.Event()
    initialize_shutdown_event(shutdown_event)

    signal_received = False
    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        nonlocal signal_received
        signal_received = True
        logger.info("Signal received, shutting down gracefully")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    async def shutdown_trigger() -> None:
        await shutdown_event.wait()
        logger.info("shutdown trigger unblocked")
        cleanup_terminal_sessions()

    await hypercorn.asyncio.serve(app, hypercorn_config, shutdown_trigger=shutdown_trigger)

    return shutdown_event.is_set() and not signal_received


if __name__ == "__main__":
    main()
