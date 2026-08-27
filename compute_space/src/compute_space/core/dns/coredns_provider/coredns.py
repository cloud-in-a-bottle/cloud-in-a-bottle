"""The CoreDNS process.  It serves two independent things, and either can run without the other:

* **Public authoritative zones** — one per public domain, only when the instance is its own DNS
  provider.  A space using an external provider serves none of these.
* **The container view** — the same names bound on the container gateway, answering the wildcard
  with the gateway IP so app containers reach sibling apps through Caddy (NAT hairpin), plus a
  catch-all forward.  Needed whenever app containers run, whoever answers publicly, because pasta
  otherwise makes the public IP local to the container netns.

Zone data reloads on an SOA serial bump, but a *new* zone means a new Corefile server block and so
a restart — see ``reload_coredns_for_domains``.  Record reads and writes go through
``service.py``; this module only seeds zone files and owns the process.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import attr
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined

from compute_space.config import Config
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.domains import effective_domains
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.logging import logger
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# StrictUndefined so a template referencing a variable/attribute we forgot to pass raises instead
# of silently rendering an empty string (e.g. a blank `file` path that CoreDNS would reject).
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), undefined=StrictUndefined)

# Fallback upstream resolvers for the container-facing DNS view's catch-all
# forward block, used only if the host's own resolvers can't be discovered.
_FALLBACK_UPSTREAM_DNS = ("8.8.8.8", "1.1.1.1")

# Monotonic SOA serial shared by every zone; serials need not relate across zones.
_SERIAL_KEY = "dns_serial"

# Zone default TTL, which the derived address records inherit.  Long by default: it is what keeps
# visitors able to reach the instance while CoreDNS is down during an update.  Dynamic DNS drops
# it, since polling every few minutes is pointless if resolvers cache the old address for five.
ADDRESS_TTL_SECONDS = 300
DYNAMIC_ADDRESS_TTL_SECONDS = 60


def _gateway_ip_is_bindable(gateway_ip: str) -> bool:
    """The ``openhost0`` dummy interface only exists on ansible-provisioned hosts; in dev/CI
    binding it would crash CoreDNS, so probe a UDP bind to decide."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind((gateway_ip, 0))
        probe.close()
        return True
    except OSError:
        return False


def _host_upstream_resolvers() -> list[str]:
    """Concrete nameservers for the container view's catch-all forward.

    Can't forward to the host's 127.0.0.53 stub (unreachable from the container netns) nor loop
    back to ourselves, so read /etc/resolv.conf and drop loopback and our own gateway.
    """
    resolvers: list[str] = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    addr = parts[1]
                    if addr.startswith("127.") or addr == CONTAINER_GATEWAY_IP or addr == "::1":
                        continue
                    resolvers.append(addr)
    except OSError:
        pass
    return resolvers or list(_FALLBACK_UPSTREAM_DNS)


def _coredns_bind_ip(public_ip: str) -> str:
    """The local address to bind for authoritative DNS.

    Wildcard :53 conflicts with podman's aardvark-dns.  The configured public IP works where it is
    assigned to an interface but fails on AWS/GCP, where public IPs are NATed to a private address;
    the default-route source is the local address that actually receives that traffic.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return public_ip


@attr.s(auto_attribs=True, frozen=True)
class DnsZone:
    """A public domain plus its zone file.  The container view's file lives next to it."""

    domain: str
    zonefile_path: Path

    @property
    def container_zonefile_path(self) -> Path:
        return self.zonefile_path.with_name(self.zonefile_path.name + ".container")


def public_dns_zones(config: Config, db: sqlite3.Connection) -> tuple[DnsZone, ...]:
    """Every non-mDNS domain, paired with its zone file path — the zones CoreDNS *can* serve.

    Whether it serves them publicly depends on ``serve_public``; the container view is generated
    for them either way.  mDNS ``.local`` domains are excluded: they are served by the wildcard
    mDNS responder, never CoreDNS/ACME.  The primary keeps the legacy ``zonefile`` path; other
    domains get a per-domain file under ``zones/``."""
    primary = primary_domain_or_none(db)
    primary_no_port = primary.name_no_port if primary else None
    return tuple(
        DnsZone(
            domain=d.name_no_port,
            zonefile_path=config.coredns_zonefile_path_for(d.name_no_port, d.name_no_port == primary_no_port),
        )
        for d in effective_domains(db)
        if not d.mdns
    )


def coredns_is_needed(zones: tuple[DnsZone, ...], serve_public: bool, container_gateway_ip: str | None) -> bool:
    """True if CoreDNS has anything to serve: public zones, or the container view (which needs a
    bindable gateway but no public zones and no public IP)."""
    if serve_public and zones:
        return True
    return container_gateway_ip is not None and _gateway_ip_is_bindable(container_gateway_ip)


def _write_coredns_config(
    zones: tuple[DnsZone, ...],
    public_ip: str | None,
    corefile_path: Path,
    container_gateway_ip: str | None,
    *,
    serve_public: bool,
    db: sqlite3.Connection | None = None,
    default_ttl: int = ADDRESS_TTL_SECONDS,
) -> None:
    """Render the Corefile plus the zone files each enabled view needs."""
    # Emitting the container view against an unbindable gateway would stop CoreDNS starting.
    if container_gateway_ip and not _gateway_ip_is_bindable(container_gateway_ip):
        logger.info("Container gateway {} not bindable; skipping container-facing DNS view", container_gateway_ip)
        container_gateway_ip = None

    if serve_public and not public_ip:
        raise ValueError("a public IP is required to serve public authoritative zones")

    corefile_path.parent.mkdir(parents=True, exist_ok=True)

    # serve_public implies public_ip (guarded above); the bind address is only meaningful when
    # there is an authoritative view to bind.
    public_zones = zones if serve_public and public_ip else ()
    container_zones = zones if container_gateway_ip else ()

    corefile_path.write_text(
        _jinja_env.get_template("Corefile").render(
            public_zones=public_zones,
            container_zones=container_zones,
            bind_ip=_coredns_bind_ip(public_ip) if public_zones and public_ip else None,
            container_gateway_ip=container_gateway_ip,
            upstream_dns=" ".join(_host_upstream_resolvers()),
        )
    )

    for zone in public_zones:
        assert public_ip is not None  # guarded above
        write_zone_file(zone, public_ip, db, default_ttl)

    for zone in container_zones:
        zone.container_zonefile_path.parent.mkdir(parents=True, exist_ok=True)
        zone.container_zonefile_path.write_text(
            _jinja_env.get_template("zonefile_container").render(
                zone_domain=zone.domain, gateway_ip=container_gateway_ip, serial=_next_serial(db)
            )
        )


def write_zone_file(
    zone: DnsZone, public_ip: str, db: sqlite3.Connection | None, default_ttl: int = ADDRESS_TTL_SECONDS
) -> None:
    """Generate a zone file from the public IP plus the records stored for it, overwriting it.

    Zone files are outputs, never inputs: nothing reads them back, so a record change or an IP move
    is a whole-file rewrite.  Written via a temp file and renamed, because CoreDNS re-reads on
    mtime change and a partial write would leave the zone unparseable.
    """
    content = _jinja_env.get_template("zonefile").render(
        zone_domain=zone.domain,
        public_ip=public_ip,
        serial=_next_serial(db),
        default_ttl=default_ttl,
        records=store.records_for(db, zone.domain) if db is not None else [],
    )
    zone.zonefile_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zone.zonefile_path.with_name(zone.zonefile_path.name + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, zone.zonefile_path)


def _next_serial(db: sqlite3.Connection | None) -> int:
    """A strictly increasing SOA serial, which is what makes CoreDNS reload the zone.

    Wall-clock alone is not enough: two writes in the same second would render the same serial and
    the second change would never be picked up.
    """
    if db is None:
        return int(time.time())
    previous = int(get_setting(db, _SERIAL_KEY) or 0)
    # Serials are unsigned 32-bit and wrap; RFC 1982 arithmetic makes the wrapped value newer.
    serial = max(previous + 1, int(time.time())) % 2**32
    set_setting(db, _SERIAL_KEY, str(serial))
    return serial


def _spawn_coredns(corefile_path: Path, coredns_bin: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [coredns_bin, "-conf", corefile_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _stream_coredns_logs(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info(f"[coredns] {line.decode(errors='replace').rstrip()}")
        proc.wait()
        logger.warning(f"CoreDNS exited with code {proc.returncode}")

    threading.Thread(target=_stream_coredns_logs, args=(proc,), daemon=True).start()
    logger.info(f"Started CoreDNS (pid {proc.pid})")
    return proc


@attr.s(auto_attribs=True)
class CoreDnsProcess:
    """Handle to the running CoreDNS child.  Mutable: restart() swaps in a fresh process so it
    picks up a regenerated Corefile.  Mirrors ``CaddyProcess``."""

    proc: subprocess.Popen[bytes]
    corefile_path: Path
    coredns_bin: str
    # Recorded so a reload regenerates the same shape of Corefile rather than silently switching
    # the instance's DNS provider.
    serve_public: bool = True
    # Insurance against a future background caller racing two coredns onto :53; today's callers are
    # already serialized on the event loop.
    _restart_lock: threading.Lock = attr.ib(factory=threading.Lock, init=False, eq=False, repr=False)

    def restart(self) -> None:
        with self._restart_lock:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logger.warning(f"CoreDNS (pid {self.proc.pid}) did not exit after terminate, killing")
                    self.proc.kill()
                    self.proc.wait()
            self.proc = _spawn_coredns(self.corefile_path, self.coredns_bin)


def start_coredns(
    zones: tuple[DnsZone, ...],
    public_ip: str | None,
    corefile_path: Path,
    container_gateway_ip: str | None = CONTAINER_GATEWAY_IP,
    coredns_bin: str = "coredns",
    *,
    serve_public: bool = True,
    db: sqlite3.Connection | None = None,
    default_ttl: int = ADDRESS_TTL_SECONDS,
) -> CoreDnsProcess:
    """Write the Corefile + zone files, start CoreDNS, and return the handle.

    ``serve_public`` controls the authoritative half — True when this instance is its own DNS
    provider — and ``public_ip`` is required for it and ignored otherwise.  ``container_gateway_ip``
    controls the container view; pass ``None`` where the gateway interface doesn't exist.
    """
    _write_coredns_config(
        zones,
        public_ip,
        corefile_path,
        container_gateway_ip,
        serve_public=serve_public,
        db=db,
        default_ttl=default_ttl,
    )
    served = ", ".join(z.domain for z in zones) or "no zones"
    logger.info(f"Starting CoreDNS ({'authoritative + ' if serve_public else ''}container view) for {served}")
    return CoreDnsProcess(
        proc=_spawn_coredns(corefile_path, coredns_bin),
        corefile_path=corefile_path,
        coredns_bin=coredns_bin,
        serve_public=serve_public,
    )


# Registered by start.py so request handlers (e.g. /api/domains) can restart CoreDNS when the
# domain set changes.  Mirrors the active-Caddy registry; None when CoreDNS isn't running.
_active_coredns: CoreDnsProcess | None = None


def set_active_coredns(coredns: CoreDnsProcess | None) -> None:
    global _active_coredns
    _active_coredns = coredns


def get_active_coredns() -> CoreDnsProcess | None:
    return _active_coredns


def reload_coredns_for_domains(config: Config, db: sqlite3.Connection) -> bool:
    """Regenerate the Corefile and zone files, and restart CoreDNS so a newly added zone is served.

    The regeneration happens either way — zone files should reflect the current domain set and IP
    whether or not anything is serving them right now.  Returns True only if CoreDNS was running
    and restarted; a restart is needed because a new zone means a new Corefile server block, and
    the bind address derives from the public IP.
    """
    public_ip = effective_public_ip(config, db)
    coredns = get_active_coredns()
    serve_public = coredns.serve_public if coredns is not None else config.coredns_enabled
    if serve_public and not public_ip:
        return False

    _write_coredns_config(
        public_dns_zones(config, db),
        public_ip,
        coredns.corefile_path if coredns is not None else config.coredns_corefile_path,
        CONTAINER_GATEWAY_IP,
        serve_public=serve_public,
        db=db,
        default_ttl=DYNAMIC_ADDRESS_TTL_SECONDS if config.dynamic_dns_enabled else ADDRESS_TTL_SECONDS,
    )
    if coredns is None:
        return False
    coredns.restart()
    return True
