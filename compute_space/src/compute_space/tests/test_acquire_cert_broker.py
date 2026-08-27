"""Tests for the openhost-cert-api broker cert-acquisition flow.

Drives the full flow against an in-process httpx.MockTransport broker and a
temp CoreDNS zone file.  No real broker, ACME server, or sleeping is involved
(a FakeClock makes the poll loop deterministic).
"""

from __future__ import annotations

import json
from pathlib import Path

import attr
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from compute_space.core.tls.acquire_cert_broker import CertAcquisitionTimeoutError
from compute_space.core.tls.acquire_cert_broker import acquire_tls_cert_via_broker
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.cert_api_client import CertApiOrderFailed
from compute_space.core.tls.keycloak import StaticTokenProvider

DOMAIN = "app.example.com"
FAKE_CHAIN = "-----BEGIN CERTIFICATE-----\nFAKECHAINBYTES\n-----END CERTIFICATE-----\n"


@attr.s(auto_attribs=True)
class FakeClock:
    """Deterministic clock: sleeping advances monotonic time, no real waiting."""

    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _write_zonefile(path: Path) -> None:
    path.write_text(
        f"$ORIGIN {DOMAIN}.\n"
        "$TTL 60\n"
        f"@   IN SOA  ns.{DOMAIN}. admin.{DOMAIN}. (\n"
        "    100   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        f"@   IN NS   ns.{DOMAIN}.\n"
        "@   IN A    127.0.0.1\n"
    )


def _order_payload() -> dict[str, object]:
    return {
        "order_id": "order-abc",
        "challenges": [
            {"domain": DOMAIN, "record_name": f"_acme-challenge.{DOMAIN}", "record_value": "base-value"},
            {"domain": f"*.{DOMAIN}", "record_name": f"_acme-challenge.{DOMAIN}", "record_value": "wildcard-value"},
        ],
    }


def _client_from_handler(handler: object) -> CertApiClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(base_url="https://broker.test", transport=transport)
    return CertApiClient(http_client=http_client, token_provider=StaticTokenProvider("tok"))


async def _noop_wait(zone_domain: str, expected_values: list[str]) -> None:
    """Stub out the real CoreDNS-reload sleep + external dig so tests stay fast."""


@attr.s(auto_attribs=True)
class _BrokerState:
    finalize_calls: int = 0
    sent_csr: str | None = None
    txt_when_first_polled: str | None = None
    waited_with: tuple[str, list[str]] | None = None


@pytest.mark.asyncio
async def test_full_flow_installs_cert_and_key(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    _write_zonefile(zonefile)

    state = _BrokerState()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            state.sent_csr = json.loads(request.read())["csr"]
            return httpx.Response(200, json=_order_payload())
        if request.url.path == "/v1/orders/order-abc/finalize":
            state.finalize_calls += 1
            if state.finalize_calls == 1:
                # The broker validates DNS; assert our TXT records are already
                # published (verbatim, absolute) and propagation was awaited
                # before we are asked to finalize.
                state.txt_when_first_polled = zonefile.read_text()
                assert state.waited_with is not None, "must wait for DNS propagation before polling finalize"
            if state.finalize_calls < 3:
                return httpx.Response(202, json={"status": "pending"})
            return httpx.Response(200, json={"status": "valid", "certificate": FAKE_CHAIN})
        return httpx.Response(404, json={"error": "not_found", "message": request.url.path})

    async def record_wait(zone_domain: str, expected_values: list[str]) -> None:
        state.waited_with = (zone_domain, expected_values)

    async with _client_from_handler(handler) as client:
        await acquire_tls_cert_via_broker(
            domain=DOMAIN,
            cert_path=cert_path,
            key_path=key_path,
            coredns_zonefile_path=zonefile,
            client=client,
            poll_interval_seconds=1.0,
            poll_timeout_seconds=600.0,
            clock=FakeClock(),
            wait_for_propagation=record_wait,
        )

    # Propagation was awaited for the base domain with both challenge values.
    assert state.waited_with == (DOMAIN, ["base-value", "wildcard-value"])

    # Polled until issued.
    assert state.finalize_calls == 3

    # The cert chain from the broker is installed verbatim.
    assert cert_path.read_text() == FAKE_CHAIN

    # The private key is a real EC key and locked down to 0600.
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)

    # Only the CSR (never the private key) was sent to the broker.
    assert state.sent_csr is not None
    assert "CERTIFICATE REQUEST" in state.sent_csr
    assert "PRIVATE KEY" not in state.sent_csr

    # TXT records were published (absolute FQDN, verbatim values) before polling.
    assert state.txt_when_first_polled is not None
    assert f'_acme-challenge.{DOMAIN}.   60  IN TXT  "base-value"' in state.txt_when_first_polled
    assert f'_acme-challenge.{DOMAIN}.   60  IN TXT  "wildcard-value"' in state.txt_when_first_polled

    # ...and cleaned up afterward.
    assert "IN TXT" not in zonefile.read_text()


@pytest.mark.asyncio
async def test_csr_covers_base_and_wildcard(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            captured["csr"] = json.loads(request.read())["csr"]
            return httpx.Response(200, json=_order_payload())
        return httpx.Response(200, json={"status": "valid", "certificate": FAKE_CHAIN})

    async with _client_from_handler(handler) as client:
        await acquire_tls_cert_via_broker(
            domain=DOMAIN,
            cert_path=tmp_path / "cert.pem",
            key_path=tmp_path / "key.pem",
            coredns_zonefile_path=zonefile,
            client=client,
            clock=FakeClock(),
            wait_for_propagation=_noop_wait,
        )

    csr = x509.load_pem_x509_csr(captured["csr"].encode())
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = san.get_values_for_type(x509.DNSName)
    assert DOMAIN in names
    assert f"*.{DOMAIN}" in names


@pytest.mark.asyncio
async def test_timeout_raises_and_clears_txt(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json=_order_payload())
        # Never finishes — always pending.
        return httpx.Response(202, json={"status": "pending"})

    async with _client_from_handler(handler) as client:
        with pytest.raises(CertAcquisitionTimeoutError):
            await acquire_tls_cert_via_broker(
                domain=DOMAIN,
                cert_path=tmp_path / "cert.pem",
                key_path=tmp_path / "key.pem",
                coredns_zonefile_path=zonefile,
                client=client,
                poll_interval_seconds=5.0,
                poll_timeout_seconds=30.0,
                clock=FakeClock(),
                wait_for_propagation=_noop_wait,
            )

    # TXT records cleaned up even on timeout.
    assert "IN TXT" not in zonefile.read_text()


@pytest.mark.asyncio
async def test_failed_order_raises_and_clears_txt(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json=_order_payload())
        # Broker drove the ACME order to a terminal failure.
        return httpx.Response(409, json={"error": "order_failed", "detail": "DNS-01 validation failed"})

    async with _client_from_handler(handler) as client:
        with pytest.raises(CertApiOrderFailed):
            await acquire_tls_cert_via_broker(
                domain=DOMAIN,
                cert_path=tmp_path / "cert.pem",
                key_path=tmp_path / "key.pem",
                coredns_zonefile_path=zonefile,
                client=client,
                clock=FakeClock(),
                wait_for_propagation=_noop_wait,
            )

    # A failed order fails fast (no full-timeout spin) and still cleans up TXT.
    assert "IN TXT" not in zonefile.read_text()
