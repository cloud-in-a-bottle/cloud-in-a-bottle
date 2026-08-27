"""Manage the VM-side CoreDNS process: the public authoritative zones and the container view.

CoreDNS serves two independent things, and either can run without the other:

* **Public authoritative zones** — one per public domain the instance answers on
  (e.g. alice.host.imbue.com plus any additional delegated domains).  Only used when the
  instance is its own DNS provider; a space using an external provider serves none of these.
* **The container view** — the same domain names bound on the container gateway, answering the
  wildcard with the gateway IP so app containers can reach sibling apps' public HTTPS URLs
  through Caddy (NAT hairpin), plus a catch-all forward upstream.  This is needed whenever app
  containers run, regardless of who provides public DNS, because pasta otherwise makes the
  public IP local to the container netns.

CoreDNS watches for SOA serial changes and auto-reloads zone data, but a *new* zone (a new
server block in the Corefile) requires a restart — see ``reload_coredns_for_domains``.

Record reads and writes go through ``compute_space.core.dns.local.LocalZoneFileBackend``, not
this module; here we only generate the initial zone files and own the process.
"""

from __future__ import annotations

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
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.zonefile import update_router_records
from compute_space.core.domains import effective_domains
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.logging import logger

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# StrictUndefined so a template referencing a variable/attribute we forgot to pass raises instead
# of silently rendering an empty string (e.g. a blank `file` path that CoreDNS would reject).
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), undefined=StrictUndefined)

# Fallback upstream resolvers for the container-facing DNS view's catch-all
# forward block, used only if the host's own resolvers can't be discovered.
_FALLBACK_UPSTREAM_DNS = ("8.8.8.8", "1.1.1.1")


def _gateway_ip_is_bindable(gateway_ip: str) -> bool:
    """True if ``gateway_ip`` is a local address CoreDNS can bind.

    The ``openhost0`` dummy interface (10.200.0.1) only exists on
    ansible-provisioned hosts; in dev/CI it won't, and binding it would crash
    CoreDNS.  Probe a UDP bind (CoreDNS serves DNS on UDP) to decide.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind((gateway_ip, 0))
        probe.close()
        return True
    except OSError:
        return False


def _host_upstream_resolvers() -> list[str]:
    """Discover the host's real upstream resolvers for the container DNS view.

    The container-facing CoreDNS view forwards non-zone queries upstream.  We
    can't forward to the host's 127.0.0.53 stub (unreachable from the container
    netns) nor loop back to ourselves, so read concrete nameservers from
    /etc/resolv.conf, dropping loopback/stub and our own gateway address.
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
    """Return the local address CoreDNS should bind for authoritative DNS.

    Binding wildcard :53 conflicts with Podman's aardvark-dns on 10.89.0.1:53.
    Binding the configured public IP works on hosts where that IP is assigned to
    an interface (for example Hetzner), but fails on AWS/GCP where public IPs are
    NATed to a private VM address. The default-route source address is the local
    interface address that receives that NATed traffic.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return public_ip


@attr.s(auto_attribs=True, frozen=True)
class DnsZone:
    """One authoritative zone CoreDNS serves: a public domain plus the path to its zone file.

    The ``.container`` view's zone file lives next to it (``container_zonefile_path``)."""

    domain: str
    zonefile_path: Path

    @property
    def container_zonefile_path(self) -> Path:
        return self.zonefile_path.with_name(self.zonefile_path.name + ".container")


def public_dns_zones(config: Config, db: sqlite3.Connection) -> tuple[DnsZone, ...]:
    """Every non-mDNS domain the instance answers on, paired with its zone file path.

    These are the zones CoreDNS *can* serve.  Whether it serves them publicly depends on
    ``serve_public`` (i.e. whether this instance is its own DNS provider); the container view is
    generated for them either way, since the hairpin is needed no matter who answers publicly.

    mDNS ``.local`` domains are excluded: they are served by the wildcard mDNS responder, never
    CoreDNS/ACME.  The primary keeps the legacy ``zonefile`` path; additional public domains get a
    per-domain file under ``zones/`` (see ``Config.coredns_zonefile_path_for``)."""
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
    """True if CoreDNS has anything to serve.

    Two independent reasons to run it: authoritative public zones, or the container view (which
    needs a bindable gateway but no public zones and no public IP).  With neither, starting it
    would bind nothing useful.
    """
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
) -> None:
    """Render the Corefile plus the zone files each enabled view needs.

    Public zone files are only *seeded* here, never regenerated: once written they are the source
    of truth for the zone's records (apps and the cert path both write into them via
    ``LocalZoneFileBackend``), so re-rendering the template over an existing file would discard
    everything but the three records the template knows about.  ``sync_public_zone`` updates the
    router-owned records of an existing file in place instead.

    Container zone files hold nothing but the router-owned records, so they are always rewritten.
    """
    bind_serial = int(time.time())

    # Only emit the container-facing view when the gateway IP is actually
    # bindable (the openhost0 dummy interface exists in production but not in
    # dev/CI), otherwise CoreDNS would fail to start.
    if container_gateway_ip and not _gateway_ip_is_bindable(container_gateway_ip):
        logger.info("Container gateway {} not bindable; skipping container-facing DNS view", container_gateway_ip)
        container_gateway_ip = None

    if serve_public and not public_ip:
        raise ValueError("a public IP is required to serve public authoritative zones")

    corefile_path.parent.mkdir(parents=True, exist_ok=True)

    # serve_public implies public_ip (guarded above), so the bind address is only computed —
    # and only meaningful — when there is an authoritative view to bind.
    public_zones = zones if serve_public and public_ip else ()
    container_zones = zones if container_gateway_ip else ()
    bind_ip = _coredns_bind_ip(public_ip) if public_zones and public_ip else None

    # Write Corefile. this is coredns's config — one server block per public zone, plus the
    # container-facing views + catch-all forward when the gateway is bindable.
    corefile = _jinja_env.get_template("Corefile").render(
        public_zones=public_zones,
        container_zones=container_zones,
        bind_ip=bind_ip,
        container_gateway_ip=container_gateway_ip,
        upstream_dns=" ".join(_host_upstream_resolvers()),
    )
    with open(corefile_path, "w") as f:
        f.write(corefile)

    for zone in public_zones:
        assert public_ip is not None  # guarded above
        zone.zonefile_path.parent.mkdir(parents=True, exist_ok=True)
        if zone.zonefile_path.exists():
            sync_public_zone(zone, public_ip)
            continue
        # Write zone file. this is the actual DNS data. CoreDNS watches for changes and auto-reloads.
        content = _jinja_env.get_template("zonefile").render(
            zone_domain=zone.domain,
            public_ip=public_ip,
            # Current timestamp as initial SOA serial: simple, and always increasing across runs.
            serial=bind_serial,
        )
        with open(zone.zonefile_path, "w") as f:
            f.write(content)

    for zone in container_zones:
        zone.container_zonefile_path.parent.mkdir(parents=True, exist_ok=True)
        container_content = _jinja_env.get_template("zonefile_container").render(
            zone_domain=zone.domain,
            gateway_ip=container_gateway_ip,
            serial=bind_serial,
        )
        with open(zone.container_zonefile_path, "w") as f:
            f.write(container_content)


def sync_public_zone(zone: DnsZone, public_ip: str) -> None:
    """Point an existing zone file's router-owned records at ``public_ip``, leaving the rest alone.

    Split out from seeding so a domain-set change or an IP update doesn't wipe records written
    through the DNS service.
    """
    update_router_records(zone.zonefile_path, zone.domain, public_ip)


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
    """Handle to the running CoreDNS child.  Mutable: restart() replaces proc with a fresh one so
    it picks up a regenerated Corefile (new zones).  Mirrors ``CaddyProcess``."""

    proc: subprocess.Popen[bytes]
    corefile_path: Path
    coredns_bin: str
    # Whether this process serves the public authoritative zones, so a reload regenerates the same
    # shape of Corefile rather than silently switching the instance's DNS provider.
    serve_public: bool = True
    # Serializes restart() to match CaddyProcess — insurance against a future background caller racing
    # two coredns onto :53 (today's callers are already serialized on the event loop).
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
) -> CoreDnsProcess:
    """Write the Corefile + zone files, start CoreDNS, and return the handle.

    ``serve_public`` controls the authoritative half: True when this instance is its own DNS
    provider, False when an external provider answers for the domains and CoreDNS is running only
    for the container view.  ``public_ip`` is required for the former and ignored by the latter.

    When ``container_gateway_ip`` is set (the default, and the dummy ``openhost0`` gateway in
    production), a server view per zone is bound there that resolves the zone wildcard to the
    gateway so pasta app containers can reach sibling apps' public HTTPS URLs through Caddy (NAT
    hairpin), with a catch-all forward for everything else.  Pass ``None`` to disable (e.g. in
    environments without the gateway interface).
    """
    _write_coredns_config(zones, public_ip, corefile_path, container_gateway_ip, serve_public=serve_public)
    served = ", ".join(z.domain for z in zones) or "no zones"
    logger.info(f"Starting CoreDNS ({'authoritative + ' if serve_public else ''}container view) for {served}")
    return CoreDnsProcess(
        proc=_spawn_coredns(corefile_path, coredns_bin),
        corefile_path=corefile_path,
        coredns_bin=coredns_bin,
        serve_public=serve_public,
    )


# The live CoreDnsProcess, registered by start.py so request handlers (e.g. /api/domains) can
# regenerate the zone config and restart CoreDNS when the domain set changes.  Mirrors the
# active-Caddy registry.  None when CoreDNS isn't running (dev / .local-only / tests).
_active_coredns: CoreDnsProcess | None = None


def set_active_coredns(coredns: CoreDnsProcess | None) -> None:
    global _active_coredns
    _active_coredns = coredns


def get_active_coredns() -> CoreDnsProcess | None:
    return _active_coredns


def reload_coredns_for_domains(config: Config, db: sqlite3.Connection) -> bool:
    """Regenerate the Corefile from the current public-domain set and restart CoreDNS so it picks
    up the new set (a new zone needs a restart; the ``file`` plugin's ``reload`` only notices edits
    to an *already-served* zone file).

    Existing zone files are re-pointed, not re-rendered — see ``_write_coredns_config``.  No-op
    (returns False) when CoreDNS isn't running, or when it serves public zones and no public IP is
    known."""
    coredns = get_active_coredns()
    if coredns is None:
        return False
    public_ip = effective_public_ip(config, db)
    if coredns.serve_public and not public_ip:
        return False
    _write_coredns_config(
        public_dns_zones(config, db),
        public_ip,
        coredns.corefile_path,
        CONTAINER_GATEWAY_IP,
        serve_public=coredns.serve_public,
    )
    coredns.restart()
    return True
