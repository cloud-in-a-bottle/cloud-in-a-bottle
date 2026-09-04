from __future__ import annotations

import asyncio
import contextlib
import os
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

# Upstream resolvers for the container-facing DNS view's catch-all, used only when the host's own
# resolvers can't be discovered.
FALLBACK_UPSTREAM_DNS = ("8.8.8.8", "1.1.1.1")

_RESOLV_CONF = Path("/etc/resolv.conf")

# The zone's default TTL, and what the records routing the space are published with.  Long by
# default: it is what keeps visitors able to reach the instance while CoreDNS is down during an
# update.
ADDRESS_TTL_SECONDS = 300


def host_upstream_resolvers(
    container_gateway_ip: str | None = None, resolv_conf: Path = _RESOLV_CONF
) -> tuple[str, ...]:
    """The host's own resolvers, for the container view's catch-all forward.

    Read from the host's resolv.conf so containers resolve through whatever the operator's network
    provides: a split-horizon corporate resolver, a VPN's, a cloud VPC's internal zone.  Hardcoding
    public resolvers instead would break every one of those, and quietly route container queries
    past the resolver the operator configured.

    Two kinds of entry are unusable and dropped.  Loopback is the systemd-resolved stub, which the
    container netns cannot reach.  Our own gateway address is this CoreDNS, so forwarding there
    would loop.  If that leaves nothing, fall back to public resolvers: containers that resolve via
    the wrong servers beat containers that resolve nothing at all.
    """
    resolvers: list[str] = []
    try:
        lines = resolv_conf.read_text().splitlines()
    except OSError:
        logger.warning(f"Could not read {resolv_conf}; forwarding container DNS to {FALLBACK_UPSTREAM_DNS}")
        return FALLBACK_UPSTREAM_DNS

    for line in lines:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "nameserver":
            continue
        addr = parts[1]
        if addr.startswith("127.") or addr == "::1" or addr == container_gateway_ip:
            continue
        resolvers.append(addr)

    if not resolvers:
        logger.info(f"No usable nameserver in {resolv_conf}; forwarding container DNS to {FALLBACK_UPSTREAM_DNS}")
        return FALLBACK_UPSTREAM_DNS
    return tuple(resolvers)


def _zonefile_path(zones_dir: Path, zone: str) -> Path:
    """Where a zone's generated file goes.

    Each zone needs its own file, since a zone is only authoritative for what is in it.  Any port
    is stripped so none ends up in a filename.
    """
    return zones_dir / f"{zone.split(':')[0]}.zone"


def _container_zonefile_path(zones_dir: Path, zone: str) -> Path:
    """The container view's copy, alongside the public one."""
    public = _zonefile_path(zones_dir, zone)
    return public.with_name(public.name + ".container")


def discard_zone_files(zones_dir: Path, zone: str) -> None:
    """Drop a removed zone's rendered files.

    Only litter once the Corefile stops referencing them, but litter that a later re-add would
    serve stale if it raced the re-render.
    """
    for path in (_zonefile_path(zones_dir, zone), _container_zonefile_path(zones_dir, zone)):
        path.unlink(missing_ok=True)


def write_coredns_config(
    zones: Sequence[str],
    records: Sequence[DnsRecord],
    serial: int,
    *,
    corefile_path: Path,
    zones_dir: Path,
    bind_ip: str | None,
    container_gateway_ip: str | None = None,
    default_ttl: int = ADDRESS_TTL_SECONDS,
    upstream_dns: Sequence[str] | None = None,
) -> None:
    """Render the Corefile plus a zone file per zone, for each enabled view.

    Builds from scratch each time, ignoring the current config.
    """
    assert bind_ip is not None or container_gateway_ip is not None, "must bind at least one view"

    # Re-read per render rather than caching at startup, so a resolver change (DHCP renewal, a VPN
    # coming up) is picked up by the next config write.
    if upstream_dns is None:
        upstream_dns = host_upstream_resolvers(container_gateway_ip)

    # The Corefile names a file per zone per view, so pair each zone up with its paths once.
    zone_files = [
        {
            "domain": zone,
            "zonefile_path": _zonefile_path(zones_dir, zone),
            "container_zonefile_path": _container_zonefile_path(zones_dir, zone),
        }
        for zone in zones
    ]

    corefile_path.parent.mkdir(parents=True, exist_ok=True)
    corefile_path.write_text(
        _jinja_env.get_template("Corefile").render(
            zones=zone_files,
            bind_ip=bind_ip,
            container_gateway_ip=container_gateway_ip,
            upstream_dns=" ".join(upstream_dns),
        )
    )

    for zone in zones:
        _write_zone_file(zone, _zonefile_path(zones_dir, zone), records, serial, default_ttl)

    # this is for the hairpin
    for zone in zones if container_gateway_ip else ():
        _write_rendered(
            _container_zonefile_path(zones_dir, zone),
            _jinja_env.get_template("zonefile_container").render(
                zone_domain=zone, gateway_ip=container_gateway_ip, serial=serial
            ),
        )


def _write_zone_file(
    zone: str,
    path: Path,
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
        path,
        _jinja_env.get_template("zonefile").render(
            zone_domain=zone, serial=serial, default_ttl=default_ttl, records=records
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
