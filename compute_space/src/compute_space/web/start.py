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
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.router_records import publish_router_addresses
from compute_space.core.domains import Domain
from compute_space.core.domains import effective_domains
from compute_space.core.first_boot import owner_exists
from compute_space.core.first_boot import seed_first_boot
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
    status = get_cert_status(config.tls_cert_path, config.tls_key_path)
    if status == CertStatus.OK:
        logger.info(f"Using existing TLS cert from {config.tls_cert_path}")
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


def _dns_bind_ip(public_ip: str) -> str:
    """The local address CoreDNS binds for authoritative DNS.

    Wildcard :53 conflicts with podman's aardvark-dns.  The configured public IP works where it is
    assigned to an interface but fails on AWS/GCP, where public IPs are NATed to a private address;
    the default-route source is the local address that actually receives that traffic.  Connecting
    a UDP socket sends nothing -- it just asks the kernel which source address the route would use.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        logger.warning(f"Default-route probe failed; binding DNS to the configured public IP {public_ip}")
        return public_ip


def _hairpin_gateway_ip() -> str | None:
    """The address the container-facing DNS view binds, or None to leave that view out.

    That view is what makes container->sibling-app hairpin work (see core/containers.py --dns).  It
    binds the ``openhost0`` dummy interface, which only ansible-provisioned hosts have; emitting a
    `bind` for an address that isn't there stops CoreDNS starting at all, so probe before asking
    for it.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((CONTAINER_GATEWAY_IP, 0))
    except OSError:
        logger.info(f"Container gateway {CONTAINER_GATEWAY_IP} not bindable; serving no container-facing DNS view")
        return None
    return CONTAINER_GATEWAY_IP


# Domains that resolve without this instance answering for them: mDNS handles ``.local``, and
# ``lvh.me`` (and friends) are public wildcards pointing at loopback.  An instance configured with
# only these has nothing to be authoritative for, so it never binds :53.
_LOCAL_ONLY_SUFFIXES = (".local", "lvh.me")


def _is_local_only(domain: Domain) -> bool:
    name = domain.name_no_port
    return domain.mdns or any(name == suffix or name.endswith(f".{suffix}") for suffix in _LOCAL_ONLY_SUFFIXES)


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
        domains = effective_domains(db)  # primary first
        _require_configured_domain(domains)  # fail loud at boot, not late in the first request

        # Bind :53 only when a domain actually needs this instance to answer for it.  With only
        # local-only domains the provider still runs, serving just the container-facing view that
        # app containers point their resolver at.
        public_zones = tuple(d.name_no_port for d in domains if not _is_local_only(d))
        public_ip: str | None = None
        bind_ip: str | None = None
        if public_zones and config.coredns_enabled:
            if not config.public_ip:
                raise RuntimeError("Public IP must be set in config to use CoreDNS")
            public_ip = config.public_ip
            bind_ip = _dns_bind_ip(public_ip)
        elif public_zones:
            logger.warning(f"coredns_enabled is off, so {', '.join(public_zones)} will not resolve via this instance")

        # Always constructed, so every caller has a provider to talk to and can be told *why* a
        # zone can't be served rather than finding a None.  Authoritative for every public domain
        # the instance answers on, so a secondary domain delegated to this box resolves too.
        dns_provider = InternalDnsProvider(
            corefile_path=config.coredns_corefile_path,
            zones_dir=config.zones_dir,
            bind_ip=bind_ip,
            container_gateway_ip=_hairpin_gateway_ip(),
            # Only resolved when it will actually be launched: looking it up self-heals by
            # downloading, which an instance that never runs CoreDNS shouldn't pay for.
            coredns_bin=_ensure_coredns_binary(config) if config.coredns_enabled else "coredns",
        )
        if bind_ip:
            await dns_provider.set_zones(public_zones)
        if public_ip is not None:
            # The provider holds its records in memory, so the ones that route the space are
            # published on every boot rather than read back from anywhere.
            publish_router_addresses(dns_provider, public_ip)

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
