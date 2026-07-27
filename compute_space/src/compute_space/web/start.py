import asyncio
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
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
from compute_space.core.dns import CoreDnsProcess
from compute_space.core.dns import public_dns_zones
from compute_space.core.dns import set_active_coredns
from compute_space.core.dns import start_coredns
from compute_space.core.domain_store import rebuild_active_domains
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.logging import logger
from compute_space.core.logging import setup_file_logging
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.core.pinned_binary import install_pinned_binary
from compute_space.core.terminal import cleanup_all as cleanup_terminal_sessions
from compute_space.core.tls.provision import provision_cert
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status
from compute_space.core.tls.renewal import start_renewal_thread
from compute_space.core.updates import RESTART_EXIT_CODE
from compute_space.core.updates import initialize_shutdown_event
from compute_space.db import init_db
from compute_space.web.app import create_app
from compute_space.web.setup_app import create_setup_app


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
    setup_file_logging(Path(os.path.dirname(config.db_path)) / "compute_space.log")
    load_keys(config.keys_dir)
    init_db(config.db_path)


def _owner_exists(config: Config) -> bool:
    db = sqlite3.connect(config.db_path)
    try:
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        db.close()


def _require_configured_domain(config: Config) -> None:
    """Check that a nonempty domain exists"""
    if not config.all_domains:
        raise RuntimeError(
            "No domain configured: nothing seeded the DB `domains` table (no first_boot.toml, no "
            "[[openhost.domains]] in the router config, and no legacy zone_domain). Set a domain in "
            "first_boot.toml or the config, then restart."
        )


def _ensure_tls_cert(config: Config) -> None:
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
            provision_cert(config)
        except Exception:
            logger.exception("TLS cert renewal failed; serving the existing cert and retrying in the background")
    else:
        provision_cert(config)


def _ensure_coredns_binary(config: Config) -> str:
    """Return the CoreDNS binary to launch, self-healing a missing one.

    Provisioning installs CoreDNS at /usr/local/bin/coredns (on the service
    PATH).  Hosts upgraded in place can lose it -- it used to come from pixi,
    which no longer ships it -- leaving ``coredns`` on no PATH directory and
    crashing startup.  When that happens, download the pinned release (same
    version as ansible/tasks/coredns.yml) into the data dir and launch it by
    absolute path.  Binding :53 works without setcap thanks to the provisioned
    net.ipv4.ip_unprivileged_port_start=25 sysctl.
    """
    found = shutil.which("coredns")
    if found:
        return found
    dest = str(config.openhost_data_path / "coredns")
    logger.warning(f"coredns not found on PATH; installing pinned release to {dest}")
    install_pinned_binary(get_pinned_binary("coredns"), dest)
    return dest


def main() -> None:
    # Allow group members to write files/dirs we create (files 664, dirs 775).
    os.umask(0o002)

    config = load_config()
    config.make_all_dirs()
    _bootstrap(config)
    # The DB `domains` table is the source of truth.  On first boot, seed it (+ the claim token)
    # from first_boot.toml, else the config-file zone_domain + [[openhost.domains]] / claim-token
    # file.  Then load the set into the active config *before* starting CoreDNS/Caddy so every
    # configured domain is served this boot.  Both are idempotent; create_app re-runs them.
    seed_first_boot(config)
    config = rebuild_active_domains(config)
    _require_configured_domain(config)  # fail loud at boot, not late in the first request
    children: list[subprocess.Popen[bytes]] = []

    coredns: CoreDnsProcess | None = None
    if config.coredns_enabled:
        if not config.public_ip:
            raise RuntimeError("Public IP must be set in config to use CoreDNS")
        # Authoritative for every public (non-mDNS) domain the instance answers on, so a
        # secondary domain delegated to this box resolves too — not just the primary.
        coredns = start_coredns(
            public_dns_zones(config),
            config.public_ip,
            config.coredns_corefile_path,
            coredns_bin=_ensure_coredns_binary(config),
        )
        # Register so /api/domains can regenerate zones + restart CoreDNS when a domain is added.
        set_active_coredns(coredns)

    if config.tls_enabled:
        _ensure_tls_cert(config)

    # Caddy reverse proxy. mainly for TLS termination, but also some other features.
    # The acquired file cert covers the primary domain (a wildcard for zone_domain);
    # any additional TLS domains fall back to Caddy's internal CA (see generate_caddyfile).
    needs_caddy_for_tls = any(d.tls for d in config.all_domains)
    caddy: CaddyProcess | None = None
    if config.start_caddy:
        caddy = start_caddy(
            config.caddyfile_path,
            config.all_domains,
            config.port,
            cert_for=config_cert_resolver(config),
        )
        # Register so /api/domains can regenerate + restart Caddy when a domain is added/removed.
        set_active_caddy(caddy)
        if needs_caddy_for_tls and config.coredns_enabled and config.acquire_tls_cert_if_missing:
            # Renew every TLS domain — including a TLS secondary under a non-TLS primary — and
            # regenerate the Caddyfile so acquired certs are served.
            start_renewal_thread(reload_caddy_for_domains)
    else:
        if needs_caddy_for_tls:
            raise RuntimeError(
                "A TLS domain is configured but start_caddy is False. Caddy is required for TLS termination."
            )

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
    if not _owner_exists(config):
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
