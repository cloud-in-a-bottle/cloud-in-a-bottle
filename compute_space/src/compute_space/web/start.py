import asyncio
import contextlib
import os
import shutil
import signal
import socket
from contextlib import closing
from pathlib import Path
from typing import Any
from typing import cast

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
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import seed_public_ip
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import DNS_SERVICE_VERSION
from compute_space.core.dns.settings import dns_settings_for
from compute_space.core.dns.settings import zones_for_domains
from compute_space.core.domains import Domain
from compute_space.core.domains import effective_domains
from compute_space.core.first_boot import owner_exists
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.logging import logger
from compute_space.core.logging import setup_file_logging
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.core.pinned_binary import install_pinned_binary
from compute_space.core.proxy_target import AsgiApp
from compute_space.core.service_interface.builtin_services import BuiltinService
from compute_space.core.service_interface.builtin_services import register_builtin_service
from compute_space.core.system_agent.client import system_agent_stop_updater_sync
from compute_space.core.system_agent.progress import mark_boot_complete
from compute_space.core.terminal import cleanup_all as cleanup_terminal_sessions
from compute_space.core.tls.renewal import start_renewal_task
from compute_space.core.updates import RESTART_EXIT_CODE
from compute_space.core.updates import initialize_shutdown_event
from compute_space.db import get_db
from compute_space.db import init_db
from compute_space.db import provide_db
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
    dns: InternalDnsProvider | None = None
    caddy: CaddyProcess | None = None
    # Long-running tasks started at boot.  Held here for their whole lifetime (the loop keeps only
    # weak references) and cancelled together by _shutdown.
    background_tasks: list[asyncio.Task[None]] = []
    # One connection for the whole domain-dependent startup sequence (CoreDNS -> TLS cert -> Caddy);
    # the primary + cert/zone paths are read live from it.
    with closing(get_db()) as db:
        domains = effective_domains(db)  # primary first
        _require_configured_domain(domains)  # fail loud at boot, not late in the first request

        # No DNS provider app installed means the router provides the `dns` service itself, so a
        # fresh instance answers its own DNS with no setup at all.
        seed_public_ip(config, db)
        public_ip = effective_public_ip(config, db)

        if config.coredns_enabled:
            if not public_ip:
                raise RuntimeError("Public IP must be set to serve authoritative DNS")
            dns = InternalDnsProvider(
                settings=dns_settings_for(config, public_ip),
                db_provider=provide_db,
                zones=zones_for_domains(db),
                coredns_bin=_ensure_coredns_binary(config),
            )
            await dns.start(db)
            # Only once it is up: the registry is what makes the router answer `dns` calls, and
            # answering before CoreDNS can serve what we accept would be a lie.
            # cast: litestar types its ASGIApp with its own scope classes, AsgiApp with the raw
            # MutableMappings; they are the same protocol.
            register_builtin_service(
                BuiltinService(service_url=DNS_SERVICE_URL, version=DNS_SERVICE_VERSION, app=cast(AsgiApp, dns.app))
            )

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

    for _ in domains:
        if config.acquire_tls_cert_if_missing:
            # Owns first acquisition as well as renewal, for every TLS domain — including a TLS
            # secondary under a non-TLS primary — and regenerates the Caddyfile once a cert
            # lands so Caddy stops serving its internal CA.

            # TODO: make this take a domain.
            background_tasks.append(start_renewal_task(reload_caddy_for_domains))

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
        for child in (caddy, dns):
            if child is not None:
                await child.stop()

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
    app = create_app(config, dns)
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
