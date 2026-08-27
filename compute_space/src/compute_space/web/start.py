import asyncio
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
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
from compute_space.core.dns import CoreDnsProcess
from compute_space.core.dns import coredns_is_needed
from compute_space.core.dns import public_dns_zones
from compute_space.core.dns import set_active_coredns
from compute_space.core.dns import start_coredns
from compute_space.core.dns import uses_local_dns
from compute_space.core.dns.dynamic import start_dynamic_dns_thread
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import seed_public_ip
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
from compute_space.core.tls.renewal import start_renewal_thread
from compute_space.core.updates import RESTART_EXIT_CODE
from compute_space.core.updates import initialize_shutdown_event
from compute_space.db import get_db
from compute_space.db import init_db
from compute_space.web.app import create_app
from compute_space.web.setup_app import create_setup_app
from openhost_system_agent.updater.paths import DATA_DIR_ENV


def _terminate_children(children: list[subprocess.Popen[bytes]]) -> None:
    for proc in children:
        if proc.poll() is None:
            logger.info(f"Terminating child process {proc.pid}")
            proc.terminate()
    for proc in children:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning(f"Child process {proc.pid} did not exit, killing")
            proc.kill()


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


def _ensure_tls_cert(config: Config, db: sqlite3.Connection) -> None:
    """Make sure a usable cert+key pair is on disk before Caddy starts, acquiring or renewing as configured."""
    status = get_cert_status(config.tls_cert_path, config.tls_key_path)
    if status == CertStatus.OK:
        logger.info(f"Using existing TLS cert from {config.tls_cert_path}")
        return
    # DNS-01 needs *a* DNS backend, not specifically CoreDNS: a space whose records live at an
    # external provider can acquire a cert with CoreDNS switched off entirely.
    local_dns = uses_local_dns(db)
    dns_available = config.coredns_enabled if local_dns else True
    if not dns_available or not config.acquire_tls_cert_if_missing:
        # A cert nearing expiry still works, so don't block startup over it.
        if status == CertStatus.EXPIRING_SOON:
            logger.warning("TLS cert expires soon but automatic cert acquisition is not enabled; cannot renew")
            return
        if not dns_available:
            raise RuntimeError(
                "This instance provides its own DNS but CoreDNS is disabled, so the DNS-01 challenge "
                "cannot be answered. Enable coredns_enabled, or install a DNS provider app."
            )
        raise RuntimeError(f"TLS cert is {status.value} and acquire_tls_cert_if_missing is False")
    if status == CertStatus.EXPIRING_SOON:
        # The existing cert is still valid, so a failed renewal shouldn't block
        # startup — the background renewal loop will keep retrying.
        try:
            provision_cert(config, db)
        except Exception:
            logger.exception("TLS cert renewal failed; serving the existing cert and retrying in the background")
    else:
        provision_cert(config, db)


def _ensure_coredns_binary(config: Config) -> str:
    """Return the CoreDNS binary to launch, self-healing a missing one."""
    if found := shutil.which("coredns"):
        return found
    dest = str(config.openhost_data_path / "coredns")
    install_pinned_binary(get_pinned_binary("coredns"), dest)
    return dest


def main() -> None:
    # Allow group members to write files/dirs we create (files 664, dirs 775).
    os.umask(0o002)

    config = load_config()
    config.make_all_dirs()
    _bootstrap(config)
    # The DB `domains` table is the source of truth.  Seed it once (+ the claim token) from
    # first_boot.toml before starting CoreDNS/Caddy so every configured domain is served this boot.
    seed_first_boot(config)
    children: list[subprocess.Popen[bytes]] = []
    coredns: CoreDnsProcess | None = None
    caddy: CaddyProcess | None = None
    # One connection for the whole domain-dependent startup sequence (CoreDNS -> TLS cert -> Caddy);
    # the primary + cert/zone paths are read live from it.
    with closing(get_db()) as db:
        domains = effective_domains(db)  # primary first
        dns_zones = public_dns_zones(config, db)
        _require_configured_domain(domains)  # fail loud at boot, not late in the first request

        # No DNS provider app installed means the router provides the `dns` service itself, so a
        # fresh instance answers its own DNS with no setup at all.
        seed_public_ip(config, db)
        public_ip = effective_public_ip(config, db)

        # Two independent reasons to run CoreDNS.  Public authoritative zones, when this instance
        # is its own DNS provider; and the container view, which the app hairpin needs whoever
        # answers publicly — including on an http-only box where coredns_enabled is false.
        serve_public = config.coredns_enabled and uses_local_dns(db)
        if serve_public and not public_ip:
            raise RuntimeError("Public IP must be set to serve authoritative DNS")
        if coredns_is_needed(dns_zones, serve_public, CONTAINER_GATEWAY_IP):
            coredns = start_coredns(
                dns_zones,
                public_ip,
                config.coredns_corefile_path,
                coredns_bin=_ensure_coredns_binary(config),
                serve_public=serve_public,
            )
            # Register so /api/domains can regenerate zones + restart CoreDNS when a domain is added.
            set_active_coredns(coredns)

        if domains[0].tls:  # primary is a TLS domain
            _ensure_tls_cert(config, db)

        # Caddy reverse proxy. mainly for TLS termination, but also some other features.
        # The acquired file cert covers the primary domain (a wildcard for it);
        # any additional TLS domains fall back to Caddy's internal CA (see generate_caddyfile).
        needs_caddy_for_tls = any(d.tls for d in domains)
        if config.start_caddy:
            # Release 80/443 from the detached updater (if a self-update just
            # happened) before Caddy binds, so Caddy can't lose the handoff race.
            system_agent_stop_updater_sync()
            caddy = start_caddy(
                config.caddyfile_path,
                domains,
                config.port,
                cert_for=config_cert_resolver(config, db),
                admin_addr=unix_admin_address(config.caddy_admin_socket_path),
            )
            # Register so /api/domains can regenerate + restart Caddy when a domain is added/removed.
            set_active_caddy(caddy)
            if needs_caddy_for_tls and (serve_public or not uses_local_dns(db)) and config.acquire_tls_cert_if_missing:
                # Renew every TLS domain — including a TLS secondary under a non-TLS primary — and
                # regenerate the Caddyfile so acquired certs are served.
                start_renewal_thread(reload_caddy_for_domains)

        if config.dynamic_dns_enabled:
            # Opt-in: on a fixed address the polling is pure cost, but on a connection that gets
            # renumbered it is the only thing that brings the space back.
            start_dynamic_dns_thread(config, get_db, config.dynamic_dns_interval_seconds)
        elif needs_caddy_for_tls:
            raise RuntimeError(
                "A TLS domain is configured but start_caddy is False. Caddy is required for TLS termination."
            )

    # Finalize the progress log only now that we're actually serving, so the
    # /updating page's "back online" doesn't fire before CoreDNS/cert/Caddy are up.
    mark_boot_complete()

    def _all_children() -> list[subprocess.Popen[bytes]]:
        # Read caddy.proc / coredns.proc at shutdown time: restart() may have replaced them.
        live = [p.proc for p in (caddy, coredns) if p is not None]
        return children + live

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
        setup_completed = asyncio.run(_serve(create_setup_app(config), hypercorn_config))
        if not setup_completed:
            logger.info("Setup interrupted by signal; exiting")
            _terminate_children(_all_children())
            time.sleep(0.1)
            os._exit(0)

    # Main web server
    app = create_app(config)
    logger.info("running hypercorn serve")
    restart_requested = asyncio.run(_serve(app, hypercorn_config))
    logger.info(f"hypercorn serve returned, restart_requested={restart_requested}")

    _terminate_children(_all_children())

    if restart_requested:
        logger.info(f"Calling os._exit({RESTART_EXIT_CODE})")
        time.sleep(0.1)
        os._exit(RESTART_EXIT_CODE)

    logger.info("Calling os._exit(0)")
    time.sleep(0.1)
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
