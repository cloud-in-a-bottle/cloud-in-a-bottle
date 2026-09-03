"""Acquire a TLS cert from the openhost-cert-api broker (DNS-01, no shared ACME creds).

The instance generates its own cert keypair + CSR locally and sends ONLY the CSR
to the broker.  The cert private key never leaves the instance — that is the whole
security point of brokering: the broker holds the ACME account and validates DNS
control, so a malicious instance cannot mint certs for domains it does not control.

Flow:
  1. generate keypair + CSR locally
  2. POST the CSR -> broker returns DNS-01 challenge record(s)
  3. publish those TXT record(s) through the router's DNS provider
  4. wait for the records to become externally visible
  5. poll finalize (202 = keep waiting) until issued, then install cert + local key
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import attr
from cryptography.hazmat.primitives import serialization

from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.logging import logger
from compute_space.core.tls import dns_challenge
from compute_space.core.tls.acquire_cert import write_cert_and_key
from compute_space.core.tls.cert_api_client import FINALIZE_STATUS_VALID
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.cert_api_client import CertApiError
from compute_space.core.tls.util import _create_csr
from compute_space.core.tls.util import _generate_tls_key
from compute_space.core.tls.util import tls_private_key_to_pem


class CertAcquisitionTimeoutError(RuntimeError):
    """The broker did not issue the certificate before the overall timeout."""


class Clock(Protocol):
    """Minimal time source so the poll loop is deterministic under test."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


@attr.s(auto_attribs=True, frozen=True)
class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


REAL_CLOCK = RealClock()


async def _wait_for_dns_propagation(domain: str, expected_values: list[str]) -> None:
    """Wait until an external resolver sees the records.

    Same safeguard the BYO-ACME path applies before validation: the broker asks the CA to validate
    during finalize, so the records must be live first or the first attempt fails.
    ``wait_until_visible`` logs and proceeds on timeout, so a delegation that never propagates still
    falls through to the broker's own retries.
    """
    await dns_challenge.wait_until_visible(domain, expected_values)


async def acquire_tls_cert_via_broker(
    domain: str,
    cert_path: Path,
    key_path: Path,
    dns_provider: InternalDnsProvider,
    client: CertApiClient,
    *,
    poll_interval_seconds: float = 5.0,
    poll_backoff_factor: float = 1.5,
    poll_max_interval_seconds: float = 30.0,
    poll_timeout_seconds: float = 600.0,
    clock: Clock = REAL_CLOCK,
    wait_for_propagation: Callable[[str, list[str]], Awaitable[None]] = _wait_for_dns_propagation,
) -> None:
    """Acquire and install a wildcard TLS cert for ``domain`` via the broker."""
    domains = [domain, f"*.{domain}"]
    logger.info(f"Acquiring TLS cert for {domains} via openhost-cert-api broker")

    # The private key stays here; only the CSR crosses the wire.
    tls_key = _generate_tls_key()
    csr_pem = _create_csr(tls_key, domains).public_bytes(serialization.Encoding.PEM).decode()

    order = await client.create_order(csr_pem)
    logger.info(f"Broker order {order.order_id} created with {len(order.challenges)} challenge(s)")

    # The broker returns a record name per challenge; a wildcard order puts both at
    # ``_acme-challenge``, the one name the provider publishes them under.
    values = [c.record_value for c in order.challenges]
    dns_challenge.publish(dns_provider, values)
    try:
        # Don't poll finalize until the records are actually live: the broker drives
        # CA validation during finalize, so a not-yet-visible record fails the order.
        await wait_for_propagation(domain, values)
        certificate = await _poll_until_issued(
            client,
            order.order_id,
            poll_interval_seconds=poll_interval_seconds,
            poll_backoff_factor=poll_backoff_factor,
            poll_max_interval_seconds=poll_max_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
            clock=clock,
        )
    finally:
        # Always pull the challenge records back out, success or failure.
        dns_challenge.clear(dns_provider)

    write_cert_and_key(cert_path, key_path, certificate.encode(), tls_private_key_to_pem(tls_key))
    logger.info(f"Installed broker-issued TLS cert for {domain} -> {cert_path}")


async def _poll_until_issued(
    client: CertApiClient,
    order_id: str,
    *,
    poll_interval_seconds: float,
    poll_backoff_factor: float,
    poll_max_interval_seconds: float,
    poll_timeout_seconds: float,
    clock: Clock,
) -> str:
    """Poll finalize until the cert is issued; raise on overall timeout.

    202/pending means "keep waiting" while the broker validates DNS and the CA
    issues.  Backoff grows the interval up to a cap, bounded by an overall deadline.
    """
    deadline = clock.monotonic() + poll_timeout_seconds
    interval = poll_interval_seconds
    while clock.monotonic() < deadline:
        result = await client.finalize_order(order_id)
        if result.status == FINALIZE_STATUS_VALID:
            if not result.certificate:
                raise CertApiError(f"Broker reported order {order_id} valid but returned no certificate")
            logger.info(f"Broker issued certificate for order {order_id}")
            return result.certificate

        sleep_for = min(interval, deadline - clock.monotonic())
        if sleep_for <= 0:
            break
        logger.info(f"Broker order {order_id} still pending; retrying in {sleep_for:.0f}s")
        await clock.sleep(sleep_for)
        interval = min(interval * poll_backoff_factor, poll_max_interval_seconds)

    raise CertAcquisitionTimeoutError(f"Broker did not issue cert for order {order_id} within {poll_timeout_seconds}s")
