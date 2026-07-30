import sqlite3
import subprocess
import threading
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


def _spawn_caddy(caddyfile_path: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        ["caddy", "run", "--config", str(caddyfile_path), "--adapter", "caddyfile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _stream_caddy_logs(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info(f"[caddy] {line.decode(errors='replace').rstrip()}")
        proc.wait()
        logger.warning(f"Caddy exited with code {proc.returncode}")

    threading.Thread(target=_stream_caddy_logs, args=(proc,), daemon=True).start()
    logger.info(f"Started Caddy (pid {proc.pid})")
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
