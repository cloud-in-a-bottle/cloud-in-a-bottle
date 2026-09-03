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
from compute_space.core.logging import logger

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# StrictUndefined so a template referencing a variable/attribute we forgot to pass raises instead
# of silently rendering an empty string (e.g. a blank `file` path that CoreDNS would reject).
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), undefined=StrictUndefined)

# upstream resolvers for the container-facing DNS view's catch-all
UPSTREAM_DNS = ("8.8.8.8", "1.1.1.1")

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


@attr.s(auto_attribs=True, frozen=True)
class DnsZone:
    """A public domain plus its zone file.  The container view's file lives next to it."""

    domain: str
    zonefile_path: Path

    @property
    def container_zonefile_path(self) -> Path:
        return self.zonefile_path.with_name(self.zonefile_path.name + ".container")


def public_dns_zones(zones_dir: Path, zones: Sequence[str]) -> tuple[DnsZone, ...]:
    """Pair each zone with the file it renders to.

    Each zone needs its own file, since a zone is only authoritative for what is in it.  Any port
    is stripped so none ends up in a filename.
    """
    return tuple(DnsZone(domain=z, zonefile_path=zones_dir / f"{z.split(':')[0]}.zone") for z in zones)


def write_coredns_config(
    zones: Sequence[DnsZone],
    records: Sequence[DnsRecord],
    serial: int,
    *,
    corefile_path: Path,
    bind_ip: str,
    container_gateway_ip: str | None = None,
    default_ttl: int = ADDRESS_TTL_SECONDS,
) -> None:
    """Render the Corefile plus a zone file per zone, for each enabled view.

    Builds from scratch each time, ignoring the current config.
    """
    # Emitting the container view against an unbindable gateway would stop CoreDNS starting.
    if container_gateway_ip and not _gateway_ip_is_bindable(container_gateway_ip):
        logger.info("Container gateway {} not bindable; skipping container-facing DNS view", container_gateway_ip)
        container_gateway_ip = None

    corefile_path.parent.mkdir(parents=True, exist_ok=True)
    corefile_path.write_text(
        _jinja_env.get_template("Corefile").render(
            zones=zones,
            bind_ip=bind_ip,
            container_gateway_ip=container_gateway_ip,
            upstream_dns=" ".join(UPSTREAM_DNS),
        )
    )

    for zone in zones:
        _write_zone_file(zone, records, serial, default_ttl)

    # this is for the hairpin
    for zone in zones if container_gateway_ip else ():
        _write_rendered(
            zone.container_zonefile_path,
            _jinja_env.get_template("zonefile_container").render(
                zone_domain=zone.domain, gateway_ip=container_gateway_ip, serial=serial
            ),
        )


def _write_zone_file(
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


@attr.s(auto_attribs=True)
class CoreDnsProcess:
    """Handle to the running CoreDNS child.  Mutable: restart() swaps in a fresh process so it
    picks up a regenerated Corefile.  Mirrors ``CaddyProcess``, including owning the log-streaming
    task that reads the process it holds."""

    proc: asyncio.subprocess.Process
    log_task: asyncio.Task[None]
    corefile_path: Path
    coredns_bin: str

    @classmethod
    async def start(cls, corefile_path: Path, coredns_bin: str = "coredns") -> CoreDnsProcess:
        """Start CoreDNS against an already-rendered Corefile, and return the handle."""
        proc = await asyncio.create_subprocess_exec(
            coredns_bin,
            "-conf",
            str(corefile_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def stream_logs() -> None:
            assert proc.stdout is not None
            async for line in proc.stdout:
                logger.info(f"[coredns] {line.decode(errors='replace').rstrip()}")
            await proc.wait()
            logger.warning(f"CoreDNS exited with code {proc.returncode}")

        log_task = asyncio.create_task(stream_logs())
        logger.info(f"Started CoreDNS (pid {proc.pid})")
        return cls(proc=proc, log_task=log_task, corefile_path=corefile_path, coredns_bin=coredns_bin)

    async def stop(self) -> None:
        """Terminate CoreDNS and wind down its log task."""
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

    async def restart(self) -> None:
        await self.stop()
        restarted = await type(self).start(self.corefile_path, self.coredns_bin)
        self.proc, self.log_task = restarted.proc, restarted.log_task
