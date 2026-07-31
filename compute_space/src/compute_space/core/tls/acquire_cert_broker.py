"""Acquire a TLS cert from the openhost-cert-api broker (DNS-01, no shared ACME creds).

The instance generates its own cert keypair + CSR locally and sends ONLY the CSR
to the broker.  The cert private key never leaves the instance — that is the whole
security point of brokering: the broker holds the ACME account and validates DNS
control, so a malicious instance cannot mint certs for domains it does not control.

Flow:
  1. generate keypair + CSR locally
  2. POST the CSR -> broker returns DNS-01 challenge record(s)
  3. publish those TXT record(s) verbatim via the existing CoreDNS write path
  4. wait for CoreDNS to reload + the records to be externally visible
  5. poll finalize (202 = keep waiting) until issued, then install cert + local key
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import attr
from cryptography.hazmat.primitives import serialization

import compute_space.core.dns as dns_module
from compute_space.core.dns import TxtRecord
from compute_space.core.logging import logger
from compute_space.core.tls.acquire_cert import write_cert_and_key
from compute_space.core.tls.cert_api_client import FINALIZE_STATUS_VALID
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.cert_api_client import CertApiError
from compute_space.core.tls.util import _create_csr
from compute_space.core.tls.util import _generate_tls_key
from compute_space.core.tls.util import _wait_for_txt_propagation
from compute_space.core.tls.util import tls_private_key_to_pem

# CoreDNS reloads the zone file on a ~2s interval; give it a beat before we expect
# the new records to be servable.  Mirrors the BYO-ACME path in core/tls/util.py.
_COREDNS_RELOAD_SECONDS = 3.0


def _as_fqdn(name: str) -> str:
    """Return ``name`` as an absolute FQDN, appending a trailing dot if missing."""
    return name if name.endswith(".") else f"{name}."


class CertAcquisitionTimeoutError(RuntimeError):
    """The broker did not issue the certificate before the overall timeout."""


class Clock(Protocol):
    """Minimal time source so the poll loop is deterministic under test."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@attr.s(auto_attribs=True, frozen=True)
class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


REAL_CLOCK = RealClock()


def _wait_for_dns_propagation(zone_domain: str, expected_values: list[str]) -> None:
    """Let CoreDNS reload, then wait until an external resolver sees the records.

    Same safeguard the BYO-ACME path applies before validation: the broker asks the
    CA to validate during finalize, so the records must be live first or the first
    attempt fails.  ``_wait_for_txt_propagation`` logs and proceeds on timeout, so a
    delegation that never propagates still falls through to the broker's own retries.
    """
    time.sleep(_COREDNS_RELOAD_SECONDS)
    _wait_for_txt_propagation(zone_domain, expected_values)


def acquire_tls_cert_via_broker(
    domain: str,
    cert_path: Path,
    key_path: Path,
    coredns_zonefile_path: Path,
    client: CertApiClient,
    *,
    poll_interval_seconds: float = 5.0,
    poll_backoff_factor: float = 1.5,
    poll_max_interval_seconds: float = 30.0,
    poll_timeout_seconds: float = 600.0,
    clock: Clock = REAL_CLOCK,
    wait_for_propagation: Callable[[str, list[str]], None] = _wait_for_dns_propagation,
) -> None:
    """Acquire and install a wildcard TLS cert for ``domain`` via the broker."""
    domains = [domain, f"*.{domain}"]
    logger.info(f"Acquiring TLS cert for {domains} via openhost-cert-api broker")

    # The private key stays here; only the CSR crosses the wire.
    tls_key = _generate_tls_key()
    csr_pem = _create_csr(tls_key, domains).public_bytes(serialization.Encoding.PEM).decode()

    order = client.create_order(csr_pem)
    logger.info(f"Broker order {order.order_id} created with {len(order.challenges)} challenge(s)")

    # Broker challenge names are full FQDNs; write them absolute (trailing dot) so
    # CoreDNS does not re-append $ORIGIN.
    records = [TxtRecord(record_name=_as_fqdn(c.record_name), record_value=c.record_value) for c in order.challenges]
    dns_module.append_txt_records(coredns_zonefile_path, records)
    try:
        # Don't poll finalize until the records are actually live: the broker drives
        # CA validation during finalize, so a not-yet-visible record fails the order.
        wait_for_propagation(domain, [c.record_value for c in order.challenges])
        certificate = _poll_until_issued(
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
        dns_module.clear_acme_challenge_records(coredns_zonefile_path)

    write_cert_and_key(cert_path, key_path, certificate.encode(), tls_private_key_to_pem(tls_key))
    logger.info(f"Installed broker-issued TLS cert for {domain} -> {cert_path}")


def _poll_until_issued(
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
        result = client.finalize_order(order_id)
        if result.status == FINALIZE_STATUS_VALID:
            if not result.certificate:
                raise CertApiError(f"Broker reported order {order_id} valid but returned no certificate")
            logger.info(f"Broker issued certificate for order {order_id}")
            return result.certificate

        sleep_for = min(interval, deadline - clock.monotonic())
        if sleep_for <= 0:
            break
        logger.info(f"Broker order {order_id} still pending; retrying in {sleep_for:.0f}s")
        clock.sleep(sleep_for)
        interval = min(interval * poll_backoff_factor, poll_max_interval_seconds)

    raise CertAcquisitionTimeoutError(f"Broker did not issue cert for order {order_id} within {poll_timeout_seconds}s")
