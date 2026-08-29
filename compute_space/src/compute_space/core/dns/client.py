"""Read and write DNS records from inside the router.

Records are named relative to the zone and land in every zone the provider serves, so callers
never handle zones at all.  ``core.service_client`` hides whether the records end up in our own
CoreDNS zone files or at a registrar, and reports a provider that is missing or down as a
``ServiceCallError`` — there is nothing to check for beforehand.

Holds nothing but the DB handle it was given, so construct one wherever you need it rather than
threading one through.

Deliberately record-level.  What a record *means* — that ``_acme-challenge`` is a DNS-01 token, or
that the apex and wildcard follow the instance's address — belongs to the caller: see
``core.tls.challenge``.

Grants are asserted per call, covering exactly the records that call touches: the narrowest thing
the router can claim, and the most useful line in a provider app's audit log.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from typing import Any

import attr

from compute_space.core.dns.service_api import ALL_ZONES
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import RecordType
from compute_space.core.dns.service_api import permission
from compute_space.core.logging import logger
from compute_space.core.service_interface.service_client import ServiceCallError
from compute_space.core.service_interface.service_client import call_service


@attr.s(auto_attribs=True, frozen=True)
class DnsClient:
    db: sqlite3.Connection

    async def set_records(self, name: str, rrtype: RecordType, values: list[str], ttl: int = 300) -> None:
        """Make ``values`` the only records at ``name``/``rrtype``, replacing whatever is there.

        ``name`` is relative to the zone, and lands in every zone the provider manages — those
        zones are aliases for one space, so there is no such thing as a record in only some of
        them.
        """
        await self._write("set", [DnsRecord(name, rrtype, ttl, v) for v in values])
        logger.info(f"Set {len(values)} {rrtype} record(s) at {name!r} in every zone")

    async def delete_records(self, name: str, rrtype: RecordType) -> None:
        """Remove every record at ``name``/``rrtype``, whatever it currently holds.

        Sends no data, which is how the service API spells "delete whatever is there" — the only
        thing a cleanup path can do when it doesn't know what a previous run wrote.
        """
        await self._write("delete", [DnsRecord(name, rrtype)])
        logger.info(f"Cleared {rrtype} records at {name!r} in every zone")

    async def _call(self, path: str, payload: dict[str, Any], permissions: list[dict[str, Any]]) -> dict[str, Any]:
        return await call_service(DNS_SERVICE_URL, path, payload, permissions, self.db)

    async def _write(self, op: str, records: list[DnsRecord]) -> None:
        payload = {"zone": ALL_ZONES, "records": [_to_wire(r) for r in records]}
        body = await self._call(f"/records/{op}", payload, [permission(r.name, r.type) for r in records])
        # Any zone reporting a failure is a failed operation, even under a partial-success 207.
        for result in body.get("results") or []:
            if isinstance(result, dict) and not result.get("ok"):
                raise ServiceCallError(f"DNS service failed for {result.get('zone')}: {result.get('error')}")


def _to_wire(record: DnsRecord) -> dict[str, Any]:
    """Data is omitted for an RRset selector: that is how the API spells "delete whatever is at
    this name and type"."""
    wire: dict[str, Any] = {"name": record.name, "type": record.type, "ttl": record.ttl}
    if record.data is not None:
        wire["data"] = record.data
    return wire


# Deliberately not the host's own resolver: with the router serving DNS that would query CoreDNS
# directly and confirm nothing about whether the delegation works.
_PROPAGATION_RESOLVER = "8.8.8.8"

_DIG_TIMEOUT_SECONDS = 10.0


async def wait_for_records(
    fqdn: str, rrtype: RecordType, expected_values: list[str], timeout: float, interval: float = 5
) -> bool:
    """Poll an external resolver until every expected value is visible at ``fqdn``.

    False on timeout, and the caller should decide whether that matters; for ACME the retry loop is
    the fallback, and some providers are slower than any timeout worth blocking on.
    """
    deadline = time.monotonic() + timeout
    expected = set(expected_values)

    while time.monotonic() < deadline:
        if await _dig_sees(fqdn, rrtype, expected):
            logger.info(f"DNS propagation confirmed for {fqdn}")
            return True
        logger.info(f"Waiting for DNS propagation of {fqdn} ({deadline - time.monotonic():.0f}s remaining)")
        await asyncio.sleep(interval)

    logger.warning(f"DNS propagation timeout for {fqdn} after {timeout}s, proceeding anyway")
    return False


async def _dig_sees(fqdn: str, rrtype: RecordType, expected: set[str]) -> bool:
    """One dig, or False if it fails — a resolver hiccup is indistinguishable from not-yet-visible
    and both mean keep waiting."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "dig",
            f"@{_PROPAGATION_RESOLVER}",
            fqdn,
            rrtype,
            "+short",
            "+timeout=5",
            "+tries=1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_DIG_TIMEOUT_SECONDS)
        except BaseException:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            # Keep reaping in the background if another cancellation arrives.
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
            raise
    except (TimeoutError, FileNotFoundError, OSError):
        return False
    return expected <= {line.strip().strip('"') for line in stdout.decode().strip().splitlines()}
