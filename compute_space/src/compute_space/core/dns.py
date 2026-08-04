"""Manage the VM-side CoreDNS zone files.

The VM runs CoreDNS authoritative for every public domain the instance answers on
(e.g. alice.host.imbue.com plus any additional delegated domains).  Each public
domain is a separate authoritative zone with its own zone file; this module writes
and updates them.  CoreDNS watches for SOA serial changes and auto-reloads the zone
data, but a *new* zone (a new server block in the Corefile) requires a CoreDNS
restart — see ``reload_coredns_for_domains``.

For DNS-01 ACME challenges, the router calls append_txt_records() on a domain's own
zone file to add TXT records, waits for CoreDNS to pick them up, then calls
clear_txt() after the cert is issued.
"""

from __future__ import annotations

import re
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
from compute_space.core.domains import effective_domains
from compute_space.core.domains import is_local_name
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.logging import logger
from compute_space.core.util import default_route_source_ip

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# StrictUndefined so a template referencing a variable/attribute we forgot to pass raises instead
# of silently rendering an empty string (e.g. a blank `file` path that CoreDNS would reject).
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), undefined=StrictUndefined)

_SERIAL_RE = re.compile(r"^(\s+)(\d+)(\s+;\s*serial\s*)$", re.MULTILINE)

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
    return default_route_source_ip() or public_ip


@attr.s(auto_attribs=True, frozen=True)
class DnsZone:
    """One authoritative zone CoreDNS serves: a public domain plus the path to its zone file.

    The ``.container`` view's zone file lives next to it (``container_zonefile_path``)."""

    domain: str
    zonefile_path: Path

    @property
    def container_zonefile_path(self) -> Path:
        return self.zonefile_path.with_name(self.zonefile_path.name + ".container")


def dns_zones(config: Config, db: sqlite3.Connection) -> tuple[DnsZone, ...]:
    """Every zone CoreDNS is authoritative for — every domain the instance answers on.

    CoreDNS serves public *and* ``.local`` domains: public names resolve to the public IP, ``.local``
    names to the box's LAN IP (so a client using CoreDNS as a conditional forwarder — e.g. Windows,
    which can't do multi-label mDNS — reaches the box).  ``.local`` never gets a public cert (keyed on
    ``tls``).  The primary keeps the legacy ``zonefile`` path; others get a per-domain file under
    ``zones/`` (see ``Config.coredns_zonefile_path_for``)."""
    primary = primary_domain_or_none(db)
    primary_no_port = primary.name_no_port if primary else None
    return tuple(
        DnsZone(
            domain=d.name_no_port,
            zonefile_path=config.coredns_zonefile_path_for(d.name_no_port, d.name_no_port == primary_no_port),
        )
        for d in effective_domains(db)
    )


def _write_coredns_config(
    zones: tuple[DnsZone, ...],
    public_ip: str,
    corefile_path: Path,
    container_gateway_ip: str | None,
    lan_ip: str | None = None,
) -> None:
    """Render the Corefile + one public (and, when applicable, container) zone file per zone.

    ``.local`` zones resolve to ``lan_ip`` (falling back to ``public_ip`` when it can't be
    discovered); public zones resolve to ``public_ip``.  The caller then spawns/re-spawns CoreDNS
    against the fresh Corefile — a new zone server block needs a restart; zone *file* edits auto-reload.
    """
    bind_serial = int(time.time())

    # Only emit the container-facing view when the gateway IP is actually
    # bindable (the openhost0 dummy interface exists in production but not in
    # dev/CI), otherwise CoreDNS would fail to start.
    if container_gateway_ip and not _gateway_ip_is_bindable(container_gateway_ip):
        logger.info("Container gateway %s not bindable; skipping container-facing DNS view", container_gateway_ip)
        container_gateway_ip = None

    corefile_path.parent.mkdir(parents=True, exist_ok=True)

    # Write Corefile. this is coredns's config — one server block per zone (plus the
    # container-facing views + catch-all forward when the gateway is bindable).
    corefile = _jinja_env.get_template("Corefile").render(
        zones=zones,
        bind_ip=_coredns_bind_ip(public_ip),
        container_gateway_ip=container_gateway_ip,
        upstream_dns=" ".join(_host_upstream_resolvers()),
    )
    with open(corefile_path, "w") as f:
        f.write(corefile)

    for zone in zones:
        # Write zone file. this is the actual DNS data. CoreDNS watches for changes and auto-reloads.
        # `.local` zones point at the LAN IP so LAN clients reach the box directly, not the public IP.
        record_ip = (lan_ip or public_ip) if is_local_name(zone.domain) else public_ip
        zone.zonefile_path.parent.mkdir(parents=True, exist_ok=True)
        content = _jinja_env.get_template("zonefile").render(
            zone_domain=zone.domain,
            record_ip=record_ip,
            # Current timestamp as initial SOA serial: simple, and always increasing across runs.
            serial=bind_serial,
        )
        with open(zone.zonefile_path, "w") as f:
            f.write(content)

        if container_gateway_ip:
            container_content = _jinja_env.get_template("zonefile_container").render(
                zone_domain=zone.domain,
                gateway_ip=container_gateway_ip,
                serial=bind_serial,
            )
            with open(zone.container_zonefile_path, "w") as f:
                f.write(container_content)


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
    public_ip: str,
    corefile_path: Path,
    container_gateway_ip: str | None = CONTAINER_GATEWAY_IP,
    coredns_bin: str = "coredns",
    lan_ip: str | None = None,
) -> CoreDnsProcess:
    """Write CoreDNS config + zone files for every domain, start CoreDNS, return the handle.

    ``.local`` zones resolve to ``lan_ip`` (see ``_write_coredns_config``).  When
    ``container_gateway_ip`` is set (the default, and the dummy ``openhost0`` gateway in
    production), a second server view per zone is bound there that resolves the zone wildcard to
    the gateway so pasta app containers can reach sibling apps' public HTTPS URLs through Caddy
    (NAT hairpin), with a catch-all forward for everything else.  Pass ``None`` to disable (e.g.
    in environments without the gateway interface).
    """
    _write_coredns_config(zones, public_ip, corefile_path, container_gateway_ip, lan_ip)
    logger.info(f"Starting CoreDNS for {', '.join(z.domain for z in zones)}")
    return CoreDnsProcess(
        proc=_spawn_coredns(corefile_path, coredns_bin),
        corefile_path=corefile_path,
        coredns_bin=coredns_bin,
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
    """Regenerate the Corefile + zone files from the config's current public-domain set and restart
    CoreDNS so it becomes authoritative for the new set (a new zone needs a restart; the ``file``
    plugin's ``reload`` only picks up edits to an *already-served* zone file).  No-op (returns
    False) when CoreDNS isn't running or no public IP is configured."""
    coredns = get_active_coredns()
    if coredns is None or not config.public_ip:
        return False
    _write_coredns_config(
        dns_zones(config, db),
        config.public_ip,
        coredns.corefile_path,
        CONTAINER_GATEWAY_IP,
        default_route_source_ip(),
    )
    coredns.restart()
    return True


def _bump_serial(content: str) -> str:
    """Read the current SOA serial from the zone file content and increment it.

    Incrementing the SOA serial is necessary to trigger CoreDNS to reload the zone file.
    """
    m = _SERIAL_RE.search(content)
    if not m:
        raise ValueError("Could not find SOA serial in zone file")
    new_serial = int(m.group(2)) + 1
    return _SERIAL_RE.sub(f"{m.group(1)}{new_serial}{m.group(3)}", content, count=1)


@attr.s(auto_attribs=True, frozen=True)
class TxtRecord:
    """A TXT record to publish.

    ``record_name`` is written into the zone file verbatim, so the caller chooses
    the addressing: a name relative to the zone's $ORIGIN (e.g. '_acme-challenge')
    has CoreDNS append the origin, while an absolute FQDN ending in '.' is honored
    as-is.
    """

    record_name: str
    record_value: str


def append_txt_records(zone_file_path: Path, records: list[TxtRecord]) -> None:
    """Append TXT record(s) to the zone file and bump the SOA serial.

    Each record's ``record_name`` is written verbatim (see TxtRecord), so this
    serves both local DNS-01 challenges (relative '_acme-challenge' names) and
    openhost-cert-api broker challenges (absolute FQDNs). Bumping the SOA serial
    triggers a CoreDNS reload.
    """
    with open(zone_file_path) as f:
        content = f.read()
    content = _bump_serial(content)
    for record in records:
        content += f'{record.record_name}   IN TXT  "{record.record_value}"\n'
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Appended {len(records)} TXT record(s)")


def clear_txt(zone_file_path: Path) -> None:
    """Remove all TXT records from the zone file and bump the SOA serial."""
    with open(zone_file_path) as f:
        content = f.read()
    lines = [line for line in content.splitlines() if "IN TXT" not in line]
    content = _bump_serial("\n".join(lines) + "\n")
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Cleared TXT records from {zone_file_path}")
