"""The CoreDNS process, and the files it reads.  It serves two things:

* **Public authoritative zones** — one per zone the compute space asked the provider to manage.
* **The container view** — the same names bound on the container gateway, answering the wildcard
  with the gateway IP so app containers reach sibling apps through Caddy (NAT hairpin), plus a
  catch-all forward.  Needed because pasta otherwise makes the public IP local to the container
  netns.

Zone data reloads on an SOA serial bump, but a *new* zone means a new Corefile server block and so
a restart — which is why ``InternalDnsProvider`` owns the process rather than this module.  Here it
is only rendering and the child process; what to render is the provider's.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Sequence
from pathlib import Path

import attr
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined

from compute_space.core.dns.coredns_provider.records import DnsRecord
from compute_space.core.dns.coredns_provider.settings import DnsSettings
from compute_space.core.logging import logger

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# StrictUndefined so a template referencing a variable/attribute we forgot to pass raises instead
# of silently rendering an empty string (e.g. a blank `file` path that CoreDNS would reject).
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), undefined=StrictUndefined)

# Fallback upstream resolvers for the container-facing DNS view's catch-all
# forward block, used only if the host's own resolvers can't be discovered.
_FALLBACK_UPSTREAM_DNS = ("8.8.8.8", "1.1.1.1")

# The zone's default TTL, and what the records routing the space are published with.  Long by
# default: it is what keeps visitors able to reach the instance while CoreDNS is down during an
# update.
ADDRESS_TTL_SECONDS = 300


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


def _host_upstream_resolvers(gateway_ip: str | None) -> list[str]:
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
                    if addr.startswith("127.") or addr == gateway_ip or addr == "::1":
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
class ManagedZone:
    """A zone the provider has been told to be authoritative for.

    ``is_primary`` only picks the zone file path -- the primary keeps the legacy one -- and says
    nothing about the records the zone carries, which are the same for every zone.
    """

    zone: str
    is_primary: bool = False


@attr.s(auto_attribs=True, frozen=True)
class DnsZone:
    """A public domain plus its zone file.  The container view's file lives next to it."""

    domain: str
    zonefile_path: Path

    @property
    def container_zonefile_path(self) -> Path:
        return self.zonefile_path.with_name(self.zonefile_path.name + ".container")


def public_dns_zones(settings: DnsSettings, zones: Sequence[ManagedZone]) -> tuple[DnsZone, ...]:
    """Pair each zone with the file it renders to."""
    return tuple(DnsZone(domain=z.zone, zonefile_path=settings.zonefile_path_for(z.zone, z.is_primary)) for z in zones)


def write_coredns_config(
    zones: Sequence[DnsZone],
    settings: DnsSettings,
    records: Sequence[DnsRecord],
    serial: int,
    default_ttl: int = ADDRESS_TTL_SECONDS,
) -> None:
    """Render the Corefile plus a zone file per zone, for each enabled view."""
    # Emitting the container view against an unbindable gateway would stop CoreDNS starting.
    container_gateway_ip = settings.container_gateway_ip
    if container_gateway_ip and not _gateway_ip_is_bindable(container_gateway_ip):
        logger.info("Container gateway {} not bindable; skipping container-facing DNS view", container_gateway_ip)
        container_gateway_ip = None

    settings.corefile_path.parent.mkdir(parents=True, exist_ok=True)
    settings.corefile_path.write_text(
        _jinja_env.get_template("Corefile").render(
            zones=zones,
            bind_ip=_coredns_bind_ip(settings.public_ip),
            container_gateway_ip=container_gateway_ip,
            upstream_dns=" ".join(_host_upstream_resolvers(container_gateway_ip)),
        )
    )

    for zone in zones:
        write_zone_file(zone, records, serial, default_ttl)

    for zone in zones if container_gateway_ip else ():
        _write_rendered(
            zone.container_zonefile_path,
            _jinja_env.get_template("zonefile_container").render(
                zone_domain=zone.domain, gateway_ip=container_gateway_ip, serial=serial
            ),
        )


def write_zone_file(
    zone: DnsZone,
    records: Sequence[DnsRecord],
    serial: int,
    default_ttl: int = ADDRESS_TTL_SECONDS,
) -> None:
    """Generate a zone file from ``records``, overwriting it.

    Only the zone's own structure is derived here -- origin, SOA, and the NS naming this instance
    -- because those are per-zone and a record is not.  Everything a resolver actually answers
    with, the addresses routing the space included, is a record.
    """
    _write_rendered(
        zone.zonefile_path,
        _jinja_env.get_template("zonefile").render(
            zone_domain=zone.domain, serial=serial, default_ttl=default_ttl, records=records
        ),
    )


def _write_rendered(path: Path, content: str) -> None:
    """Zone files are outputs, never inputs: nothing reads them back, so any change is a whole-file
    rewrite.  Written via a temp file and renamed, because CoreDNS re-reads on mtime change and a
    partial write would leave the zone unparseable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


async def _spawn_coredns(
    corefile_path: Path, coredns_bin: str
) -> tuple[asyncio.subprocess.Process, asyncio.Task[None]]:
    proc = await asyncio.create_subprocess_exec(
        coredns_bin,
        "-conf",
        str(corefile_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _stream_coredns_logs() -> None:
        assert proc.stdout is not None
        async for line in proc.stdout:
            logger.info(f"[coredns] {line.decode(errors='replace').rstrip()}")
        await proc.wait()
        logger.warning(f"CoreDNS exited with code {proc.returncode}")

    log_task = asyncio.create_task(_stream_coredns_logs())
    logger.info(f"Started CoreDNS (pid {proc.pid})")
    return proc, log_task


@attr.s(auto_attribs=True)
class CoreDnsProcess:
    """Handle to the running CoreDNS child.  Mutable: restart() swaps in a fresh process so it
    picks up a regenerated Corefile.  Mirrors ``CaddyProcess``, including owning the log-streaming
    task that reads the process it holds."""

    proc: asyncio.subprocess.Process
    log_task: asyncio.Task[None]
    corefile_path: Path
    coredns_bin: str
    # Insurance against a future background caller racing two coredns onto :53; today's callers are
    # already serialized on the event loop.
    _restart_lock: asyncio.Lock = attr.ib(factory=asyncio.Lock, init=False, eq=False, repr=False)

    async def _stop_locked(self) -> None:
        """Terminate CoreDNS and wind down its log task.  Caller must hold the lock."""
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except TimeoutError:
                logger.warning(f"CoreDNS (pid {self.proc.pid}) did not exit after terminate, killing")
                self.proc.kill()
                await self.proc.wait()
        # An unprompted exit is worth logging, so the task logs it itself; a deliberate stop isn't.
        self.log_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.log_task

    async def stop(self) -> None:
        """Shut CoreDNS down for good."""
        async with self._restart_lock:
            await self._stop_locked()

    async def restart(self) -> None:
        async with self._restart_lock:
            await self._stop_locked()
            self.proc, self.log_task = await _spawn_coredns(self.corefile_path, self.coredns_bin)


async def start_coredns(settings: DnsSettings, coredns_bin: str = "coredns") -> CoreDnsProcess:
    """Start CoreDNS against an already-rendered Corefile, and return the handle."""
    proc, log_task = await _spawn_coredns(settings.corefile_path, coredns_bin)
    return CoreDnsProcess(
        proc=proc,
        log_task=log_task,
        corefile_path=settings.corefile_path,
        coredns_bin=coredns_bin,
    )
