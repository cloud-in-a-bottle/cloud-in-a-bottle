import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import attr

from compute_space.config import Config
from compute_space.core.domains import Domain
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger

# Resolver: given a domain name, return its (cert_path, key_path) if a real cert file exists
# on disk, else None (→ Caddy's internal self-signed CA).  Lets a domain that has an acquired
# cert use it while one still being acquired falls back to `tls internal`.
CertResolver = Callable[[str], tuple[Path, Path] | None]

# `{host}` / `{uri}` are Caddy request placeholders — kept out of the f-strings so
# they survive verbatim into the generated Caddyfile.
_REDIRECT_BLOCK = "    redir https://{host}{uri} permanent\n"


def _tls_domain_blocks(name: str, tls_directive: str, web_server_port: int) -> str:
    """https for `name` + `*.name` (proxied to the router), and an http site that
    redirects to https.  Scoping the redirect to this domain's http site — rather
    than a global `:80` catch-all — is what lets a sibling `.local` domain stay on
    plain http instead of being bounced to https."""
    return (
        f"https://{name}, https://*.{name} {{\n"
        f"    {tls_directive}\n"
        "    encode gzip zstd\n"
        f"    reverse_proxy localhost:{web_server_port}\n"
        "}\n"
        f"http://{name}, http://*.{name} {{\n"
        f"{_REDIRECT_BLOCK}"
        "}\n"
    )


def _http_domain_block(name: str, web_server_port: int) -> str:
    """Plain http for `name` + `*.name`, proxied to the router with NO redirect —
    used for mDNS `.local` domains that are served over http."""
    return (
        f"http://{name}, http://*.{name} {{\n    encode gzip zstd\n    reverse_proxy localhost:{web_server_port}\n}}\n"
    )


def config_cert_resolver(config: Config, db: sqlite3.Connection) -> CertResolver:
    """A CertResolver backed by the config's on-disk cert layout: a domain uses its file
    cert (the primary's legacy path, or a per-domain ``certs/<name>`` pair) when both files
    exist, otherwise falls back to ``tls internal``."""

    def resolve(name: str) -> tuple[Path, Path] | None:
        cert_path, key_path = config.cert_key_paths_for(db, name)
        if cert_path.exists() and key_path.exists():
            return (cert_path, key_path)
        return None

    return resolve


def generate_caddyfile(
    domains: tuple[Domain, ...],
    web_server_port: int,
    cert_for: CertResolver | None = None,
    admin_addr: str | None = None,
) -> str:
    """Generate Caddyfile content for the full domain set — one site block per domain.

    A TLS domain serves https (+ http→https redirect); it uses its acquired file cert when
    ``cert_for`` resolves one, otherwise Caddy's internal self-signed CA (``tls internal``) —
    which lets an extra domain come up for local testing, or serve immediately while its real
    cert is still being acquired.  A non-TLS (mDNS ``.local``) domain serves plain http with no
    redirect, so those requests are never forced to https.  All blocks reverse-proxy to the
    router on loopback.  ``admin_addr`` sets the admin endpoint (for zero-downtime reloads);
    ``None`` disables it (``admin off``).
    """
    resolve = cert_for or (lambda _name: None)
    has_tls = any(d.tls for d in domains)
    # `disable_redirects` (not `off`) so Caddy's internal CA can still issue certs
    # for `tls internal` domains; the per-domain http blocks above provide the
    # http→https redirects we want, and only for the domains that want them.
    auto_https = "disable_redirects" if has_tls else "off"
    parts = [f"{{\n    auto_https {auto_https}\n    admin {admin_addr or 'off'}\n}}\n"]
    for d in domains:
        name = d.name_no_port
        if not d.tls:
            parts.append(_http_domain_block(name, web_server_port))
        elif paths := resolve(name):
            parts.append(_tls_domain_blocks(name, f"tls {paths[0]} {paths[1]}", web_server_port))
        else:
            parts.append(_tls_domain_blocks(name, "tls internal", web_server_port))
    return "".join(parts)


def unix_admin_address(socket_path: Path) -> str:
    """Caddy network address for a unix-socket admin endpoint (``unix/`` + the absolute path)."""
    return f"unix/{socket_path}"


# During a self-update the detached updater (openhost_system_agent.updater) holds
# :443/:80 to serve the "updating" page while compute_space is down, and releases
# them once this new compute_space is listening on loopback. There is a brief
# window where the updater hasn't let go yet, so a fresh Caddy can hit
# "address already in use". Retry the spawn for a few seconds to ride out that
# handoff instead of leaving the instance with no front proxy.
_CADDY_BIND_RETRY_SECONDS = 10.0
_CADDY_BIND_RETRY_INTERVAL = 0.25
_CADDY_ADDR_IN_USE = "address already in use"


def _spawn_caddy_once(caddyfile_path: Path) -> tuple[subprocess.Popen[bytes], list[str], threading.Thread]:
    """Spawn Caddy and stream its logs. Returns the proc, a mutable list that
    accumulates the most recent output lines (for post-exit diagnosis), and the
    log-streaming thread (so callers can join it to be sure output is drained
    before inspecting the lines)."""
    proc = subprocess.Popen(
        ["caddy", "run", "--config", str(caddyfile_path), "--adapter", "caddyfile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Bounded tail of recent Caddy output so _spawn_caddy can tell a bind conflict
    # (retryable during the update handoff) from a real config error (fail fast).
    recent: list[str] = []

    def _stream_caddy_logs(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            recent.append(text)
            del recent[:-20]  # keep only the last ~20 lines
            logger.info(f"[caddy] {text}")
        proc.wait()
        logger.warning(f"Caddy exited with code {proc.returncode}")

    log_thread = threading.Thread(target=_stream_caddy_logs, args=(proc,), daemon=True)
    log_thread.start()
    logger.info(f"Started Caddy (pid {proc.pid})")
    return proc, recent, log_thread


def _spawn_caddy(caddyfile_path: Path) -> subprocess.Popen[bytes]:
    """Start Caddy, retrying briefly ONLY if :443/:80 is still held (update handoff).

    Caddy binds its listeners synchronously and exits non-zero on a bind conflict,
    printing "address already in use". We retry just that case (the updater is
    still releasing the ports); any other immediate exit (e.g. a config error) is
    returned right away so the caller fails fast instead of spinning.
    """
    deadline = time.monotonic() + _CADDY_BIND_RETRY_SECONDS
    while True:
        proc, recent, log_thread = _spawn_caddy_once(caddyfile_path)
        # Give Caddy a moment to either bind or fail on the ports. A successful
        # Caddy stays alive; a bind conflict exits within ~this window.
        try:
            proc.wait(timeout=_CADDY_BIND_RETRY_INTERVAL)
        except subprocess.TimeoutExpired:
            # Still running after the settle window — it bound successfully.
            return proc
        # Caddy exited. Wait for the log thread to finish draining stdout (EOF on
        # exit makes this quick) so ``recent`` is complete before we classify the
        # failure — otherwise a bind conflict could be misread as a config error.
        log_thread.join(timeout=2.0)
        # Retry only if it was a port conflict (the updater hasn't released
        # 443/80 yet) and we still have time.
        addr_in_use = any(_CADDY_ADDR_IN_USE in line for line in recent)
        if addr_in_use and time.monotonic() < deadline:
            logger.info("Caddy bind conflict (ports still held by the update server); retrying")
            time.sleep(_CADDY_BIND_RETRY_INTERVAL)
            continue
        # A non-bind failure, or out of retries: return the (dead) proc so the
        # caller surfaces the failure rather than silently believing Caddy is up.
        if not addr_in_use:
            logger.warning("Caddy exited immediately for a non-bind reason; not retrying")
        else:
            logger.warning("Caddy failed to bind within the update-handoff retry window")
        return proc


@attr.s(auto_attribs=True)
class CaddyProcess:
    """Handle to the running Caddy child.  Mutable: restart()/reload() may replace proc."""

    proc: subprocess.Popen[bytes]
    caddyfile_path: Path
    # Admin API address (Caddy network form, e.g. `unix//path`) for zero-downtime reloads; None
    # means Caddy runs with `admin off`, so reload() falls back to a cold restart.
    admin_addr: str | None = None
    # Serializes restart()/reload(): several daemon threads (deferred domain reload, cert-acquisition
    # completion, TLS renewal) can call at once, and two overlapping restarts race :443.
    _restart_lock: threading.Lock = attr.ib(factory=threading.Lock, init=False, eq=False, repr=False)

    def _cold_restart_locked(self) -> None:
        """Stop the current process (if alive) and spawn a fresh one.  Caller must hold the lock; the
        old process must exit before the new one starts since both bind :80/:443."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning(f"Caddy (pid {self.proc.pid}) did not exit after terminate, killing")
                self.proc.kill()
                self.proc.wait()
        # A killed Caddy may leave its admin unix socket behind, blocking rebind; clear it first.
        if self.admin_addr and self.admin_addr.startswith("unix/"):
            Path(self.admin_addr.removeprefix("unix/")).unlink(missing_ok=True)
        self.proc = _spawn_caddy(self.caddyfile_path)

    def restart(self) -> None:
        """Cold restart (terminate + respawn), dropping in-flight connections.  Prefer reload()."""
        with self._restart_lock:
            self._cold_restart_locked()

    def reload(self) -> None:
        """Apply the current Caddyfile with a zero-downtime graceful reload via the admin API, so
        in-flight requests (including the request that triggered a domain change) aren't dropped.
        Falls back to a cold restart if the admin API is off, Caddy is dead, or the reload fails."""
        with self._restart_lock:
            if self.admin_addr is None or self.proc.poll() is not None:
                self._cold_restart_locked()
                return
            try:
                result = subprocess.run(
                    [
                        "caddy",
                        "reload",
                        "--config",
                        str(self.caddyfile_path),
                        "--adapter",
                        "caddyfile",
                        "--address",
                        self.admin_addr,
                    ],
                    capture_output=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                logger.error("caddy reload timed out after 30s; cold-restarting")
                self._cold_restart_locked()
                return
            if result.returncode != 0:
                logger.error(
                    f"caddy reload failed (rc={result.returncode}): "
                    f"{result.stderr.decode(errors='replace').strip()}; cold-restarting"
                )
                self._cold_restart_locked()


def start_caddy(
    caddyfile_path: Path,
    domains: tuple[Domain, ...],
    web_server_port: int,
    cert_for: CertResolver | None = None,
    admin_addr: str | None = None,
) -> CaddyProcess:
    """Generate Caddyfile and start Caddy."""
    caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    caddyfile_path.write_text(generate_caddyfile(domains, web_server_port, cert_for, admin_addr))
    return CaddyProcess(proc=_spawn_caddy(caddyfile_path), caddyfile_path=caddyfile_path, admin_addr=admin_addr)


# The live CaddyProcess, registered by start.py so request handlers (e.g. /api/domains) can
# regenerate the Caddyfile and restart Caddy when the domain set changes.  Mirrors the
# config._active_config pattern.  None when Caddy isn't running (dev / .local-only / tests).
_active_caddy: CaddyProcess | None = None


def set_active_caddy(caddy: CaddyProcess | None) -> None:
    global _active_caddy
    _active_caddy = caddy


def get_active_caddy() -> CaddyProcess | None:
    return _active_caddy


def reload_caddy_for_domains(config: Config, db: sqlite3.Connection) -> bool:
    """Regenerate the Caddyfile from the current domain set and gracefully reload Caddy so it serves
    the new set with zero downtime.  No-op (returns False) when Caddy isn't running — the domain set
    still changed in the DB; there's just no front proxy to reload (dev / .local-only)."""
    caddy = get_active_caddy()
    if caddy is None:
        return False
    caddy.caddyfile_path.write_text(
        generate_caddyfile(effective_domains(db), config.port, config_cert_resolver(config, db), caddy.admin_addr)
    )
    caddy.reload()
    return True
