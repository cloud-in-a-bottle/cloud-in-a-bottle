"""Has a record actually made it out?

Asks a real resolver, which is the only thing that answers the question: writing a record into a
zone file says nothing about whether CoreDNS has reloaded, or whether the parent zone's NS
delegation points here at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence

from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.logging import logger

# Deliberately not the host's own resolver: with the router serving DNS that would query CoreDNS
# directly and confirm nothing about whether the delegation works.
_PROPAGATION_RESOLVER = "8.8.8.8"

_DIG_TIMEOUT_SECONDS = 10.0


async def wait_for_records(
    fqdn: str, record_type: RecordType, expected_values: Sequence[str], timeout: float, interval: float = 5
) -> bool:
    """Poll an external resolver until every expected value is visible at ``fqdn``.

    False on timeout, and the caller should decide whether that matters; for ACME the retry loop is
    the fallback, and some delegations are slower than any timeout worth blocking on.
    """
    deadline = time.monotonic() + timeout
    expected = set(expected_values)

    while time.monotonic() < deadline:
        if await _dig_sees(fqdn, record_type, expected):
            logger.info(f"DNS propagation confirmed for {fqdn}")
            return True
        logger.info(f"Waiting for DNS propagation of {fqdn} ({deadline - time.monotonic():.0f}s remaining)")
        await asyncio.sleep(interval)

    logger.warning(f"DNS propagation timeout for {fqdn} after {timeout}s, proceeding anyway")
    return False


async def _dig_sees(fqdn: str, record_type: RecordType, expected: set[str]) -> bool:
    """One dig, or False if it fails -- a resolver hiccup is indistinguishable from not-yet-visible
    and both mean keep waiting."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "dig",
            f"@{_PROPAGATION_RESOLVER}",
            fqdn,
            record_type,
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
