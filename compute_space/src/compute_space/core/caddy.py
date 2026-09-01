import asyncio
import contextlib
import sqlite3
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


# Retry the upstream for a few seconds instead of 502ing: post-restart Caddy can
# bind 443 a beat before the router's loopback listener is up.
def _reverse_proxy(web_server_port: int) -> str:
    return (
        f"    reverse_proxy localhost:{web_server_port} {{\n"
        "        lb_try_duration 10s\n"
        "        lb_try_interval 250ms\n"
        "    }\n"
    )


def _tls_domain_blocks(name: str, tls_directive: str, web_server_port: int) -> str:
    """https for `name` + `*.name` (proxied to the router), and an http site that
    redirects to https.  Scoping the redirect to this domain's http site — rather
    than a global `:80` catch-all — is what lets a sibling `.local` domain stay on
    plain http instead of being bounced to https."""
    return (
        f"https://{name}, https://*.{name} {{\n"
        f"    {tls_directive}\n"
        "    encode gzip zstd\n"
        f"{_reverse_proxy(web_server_port)}"
        "}\n"
        f"http://{name}, http://*.{name} {{\n"
        f"{_REDIRECT_BLOCK}"
        "}\n"
    )


def _http_domain_block(name: str, web_server_port: int) -> str:
    """Plain http for `name` + `*.name`, proxied to the router with NO redirect —
    used for mDNS `.local` domains that are served over http."""
    return f"http://{name}, http://*.{name} {{\n    encode gzip zstd\n{_reverse_proxy(web_server_port)}}}\n"


def config_cert_resolver(config: Config, db: sqlite3.Connection) -> CertResolver:
    """A CertResolver backed by the config's on-disk cert layout: a domain uses its file
    cert (the original primary's legacy path, or a per-domain ``certs/<name>`` pair) when both files
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
    # Serve only h1/h2 (both TCP): the update-downtime server covers TCP 80/443
    # but not HTTP/3's UDP :443, so advertising h3 would make browsers try QUIC
    # and hit ERR_QUIC_PROTOCOL_ERROR while the updater holds the ports.
    parts = [
        f"{{\n    auto_https {auto_https}\n    admin {admin_addr or 'off'}\n    servers {{\n        protocols h1 h2\n    }}\n}}\n"
    ]
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


# The detached updater holds :443/:80 until this new compute_space is up, so a
# fresh Caddy can briefly hit "address already in use". Retry to ride out the
# handoff. The window is generous: a slow bind is far better than no TLS.
_CADDY_BIND_RETRY_SECONDS = 90.0
_CADDY_BIND_RETRY_INTERVAL = 0.25
_CADDY_ADDR_IN_USE = "address already in use"


async def _spawn_caddy_once(caddyfile_path: Path) -> tuple[asyncio.subprocess.Process, list[str], asyncio.Task[None]]:
    """Spawn Caddy and stream its logs. Returns the proc, a bounded tail of recent
    output lines (so _spawn_caddy can tell a bind conflict from a config error),
    and the log task."""
    proc = await asyncio.create_subprocess_exec(
        "caddy",
        "run",
        "--config",
        str(caddyfile_path),
        "--adapter",
        "caddyfile",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    recent: list[str] = []

    async def _stream_caddy_logs() -> None:
        assert proc.stdout is not None
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            recent.append(text)
            del recent[:-20]
            logger.info(f"[caddy] {text}")
        await proc.wait()
        logger.warning(f"Caddy exited with code {proc.returncode}")

    log_task = asyncio.create_task(_stream_caddy_logs())
    logger.info(f"Started Caddy (pid {proc.pid})")
    return proc, recent, log_task


async def _spawn_caddy(caddyfile_path: Path) -> tuple[asyncio.subprocess.Process, asyncio.Task[None]]:
    """Start Caddy, retrying only the "address already in use" case while :443/:80
    is still held by the update-downtime server. Any other immediate exit (e.g. a
    config error) is returned right away so the caller fails fast.

    Returns the process and its log task; the caller owns both (see CaddyProcess).
    """
    deadline = time.monotonic() + _CADDY_BIND_RETRY_SECONDS
    while True:
        proc, recent, log_task = await _spawn_caddy_once(caddyfile_path)
        await asyncio.sleep(_CADDY_BIND_RETRY_INTERVAL)
        if proc.returncode is None:
            return proc, log_task  # still running after the settle window — bound successfully
        # Drain the log task before classifying, else a bind conflict could look
        # like a config error.  Cancel covers the drain timing out; it's a no-op otherwise.
        await asyncio.wait([log_task], timeout=2.0)
        log_task.cancel()
        addr_in_use = any(_CADDY_ADDR_IN_USE in line for line in recent)
        if addr_in_use and time.monotonic() < deadline:
            logger.info("Caddy bind conflict (ports still held by the update server); retrying")
            await asyncio.sleep(_CADDY_BIND_RETRY_INTERVAL)
            continue
        if not addr_in_use:
            logger.warning("Caddy exited immediately for a non-bind reason; not retrying")
        else:
            logger.warning("Caddy failed to bind within the update-handoff retry window")
        return proc, log_task


_CADDY_RELOAD_TIMEOUT = 30.0


async def _run_caddy_reload(caddyfile_path: Path, admin_addr: str) -> tuple[int, bytes]:
    """Run ``caddy reload`` against the admin endpoint, returning (returncode, stderr).  Raises
    TimeoutError — after killing the hung child, so it can't outlive the cold restart that
    follows — if it doesn't finish in time."""
    proc = await asyncio.create_subprocess_exec(
        "caddy",
        "reload",
        "--config",
        str(caddyfile_path),
        "--adapter",
        "caddyfile",
        "--address",
        admin_addr,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CADDY_RELOAD_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    assert proc.returncode is not None
    return proc.returncode, stderr


@attr.s(auto_attribs=True)
class CaddyProcess:
    """Handle to the running Caddy child.  Mutable: restart()/reload() may replace proc.

    Owns the log-streaming task alongside the process it reads — the two share a lifetime, and the
    loop keeps only a weak reference to a task, so something has to hold a strong one."""

    proc: asyncio.subprocess.Process
    log_task: asyncio.Task[None]
    caddyfile_path: Path
    # Admin API address (Caddy network form, e.g. `unix//path`) for zero-downtime reloads; None
    # means Caddy runs with `admin off`, so reload() falls back to a cold restart.
    admin_addr: str | None = None
    # Serializes restart()/reload(): the domain API, cert acquisition and TLS renewal can all call
    # at once, and two overlapping restarts race :443.
    _restart_lock: asyncio.Lock = attr.ib(factory=asyncio.Lock, init=False, eq=False, repr=False)

    async def _stop_locked(self) -> None:
        """Terminate Caddy and wind down its log task.  Caller must hold the lock."""
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except TimeoutError:
                logger.warning(f"Caddy (pid {self.proc.pid}) did not exit after terminate, killing")
                self.proc.kill()
                await self.proc.wait()
        # An unprompted exit is worth logging, so the task logs it itself; a deliberate stop isn't.
        self.log_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.log_task

    async def _cold_restart_locked(self) -> None:
        """Stop the current process (if alive) and spawn a fresh one.  Caller must hold the lock; the
        old process must exit before the new one starts since both bind :80/:443."""
        await self._stop_locked()
        # A killed Caddy may leave its admin unix socket behind, blocking rebind; clear it first.
        if self.admin_addr and self.admin_addr.startswith("unix/"):
            Path(self.admin_addr.removeprefix("unix/")).unlink(missing_ok=True)
        self.proc, self.log_task = await _spawn_caddy(self.caddyfile_path)

    async def stop(self) -> None:
        """Shut Caddy down for good."""
        async with self._restart_lock:
            await self._stop_locked()

    async def restart(self) -> None:
        """Cold restart (terminate + respawn), dropping in-flight connections.  Prefer reload()."""
        async with self._restart_lock:
            await self._cold_restart_locked()

    async def reload(self) -> None:
        """Apply the current Caddyfile with a zero-downtime graceful reload via the admin API, so
        in-flight requests (including the request that triggered a domain change) aren't dropped.
        Falls back to a cold restart if the admin API is off, Caddy is dead, or the reload fails."""
        async with self._restart_lock:
            if self.admin_addr is None or self.proc.returncode is not None:
                await self._cold_restart_locked()
                return
            try:
                returncode, stderr = await _run_caddy_reload(self.caddyfile_path, self.admin_addr)
            except TimeoutError:
                logger.error(f"caddy reload timed out after {_CADDY_RELOAD_TIMEOUT:.0f}s; cold-restarting")
                await self._cold_restart_locked()
                return
            if returncode != 0:
                logger.error(
                    f"caddy reload failed (rc={returncode}): "
                    f"{stderr.decode(errors='replace').strip()}; cold-restarting"
                )
                await self._cold_restart_locked()


async def start_caddy(
    caddyfile_path: Path,
    domains: tuple[Domain, ...],
    web_server_port: int,
    cert_for: CertResolver | None = None,
    admin_addr: str | None = None,
) -> CaddyProcess:
    """Generate Caddyfile and start Caddy."""
    caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    caddyfile_path.write_text(generate_caddyfile(domains, web_server_port, cert_for, admin_addr))
    proc, log_task = await _spawn_caddy(caddyfile_path)
    return CaddyProcess(proc=proc, log_task=log_task, caddyfile_path=caddyfile_path, admin_addr=admin_addr)


# The live CaddyProcess, registered by start.py so request handlers (e.g. /api/domains) can
# regenerate the Caddyfile and restart Caddy when the domain set changes.  Mirrors the
# config._active_config pattern.  None when Caddy isn't running (dev / .local-only / tests).
_active_caddy: CaddyProcess | None = None


def set_active_caddy(caddy: CaddyProcess | None) -> None:
    global _active_caddy
    _active_caddy = caddy


def get_active_caddy() -> CaddyProcess | None:
    return _active_caddy


async def reload_caddy_for_domains(config: Config, db: sqlite3.Connection) -> bool:
    """Regenerate the Caddyfile from the current domain set and gracefully reload Caddy so it serves
    the new set with zero downtime.  No-op (returns False) when Caddy isn't running — the domain set
    still changed in the DB; there's just no front proxy to reload (dev / .local-only)."""
    caddy = get_active_caddy()
    if caddy is None:
        return False
    caddy.caddyfile_path.write_text(
        generate_caddyfile(effective_domains(db), config.port, config_cert_resolver(config, db), caddy.admin_addr)
    )
    await caddy.reload()
    return True
