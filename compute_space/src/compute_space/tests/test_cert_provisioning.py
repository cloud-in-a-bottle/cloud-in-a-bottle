"""The single funnel every TLS cert acquisition goes through."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.tls import provision
from compute_space.core.tls.provision import acquire_cert_for_domain


def _dns(tmp_path: Path, bind_ip: str | None = "10.0.0.5") -> InternalDnsProvider:
    """Never started: every acquisition that would touch CoreDNS is patched out."""
    return InternalDnsProvider(corefile_path=tmp_path / "Corefile", zones_dir=tmp_path / "zones", bind_ip=bind_ip)


def _config(tmp_path: Path) -> DefaultConfig:
    return DefaultConfig(data_root_dir=str(tmp_path), acme_account_key_path=str(tmp_path / "acme.key"))


async def _run_two_overlapping_acquisitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Start a second acquisition while the first is mid-flight; return how many ran at once."""
    config, dns_provider = _config(tmp_path), _dns(tmp_path)
    in_flight = 0
    high_water = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_acquire(**kwargs: Any) -> None:
        nonlocal in_flight, high_water
        in_flight += 1
        high_water = max(high_water, in_flight)
        entered.set()
        # Where a real acquisition sits between publishing its tokens and cleaning them up.
        await release.wait()
        in_flight -= 1

    monkeypatch.setattr(provision, "acquire_tls_cert", fake_acquire)

    async def acquire(domain: str) -> None:
        await acquire_cert_for_domain(
            config, domain, tmp_path / f"{domain}.crt", tmp_path / f"{domain}.key", None, dns_provider
        )

    first = asyncio.create_task(acquire("a.example.com"))
    await entered.wait()
    second = asyncio.create_task(acquire("b.example.com"))
    # Let the second run as far as it can get: up to the lock, or straight into the acquisition.
    for _ in range(10):
        await asyncio.sleep(0)

    release.set()
    await asyncio.gather(first, second)
    return high_water


@pytest.mark.asyncio
async def test_acquisitions_do_not_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every DNS-01 challenge is answered from the same _acme-challenge name, so a second
    # acquisition starting mid-flight would overwrite the first one's tokens and then clear them
    # on its way out -- failing a renewal that had minutes of polling left to run.
    assert await _run_two_overlapping_acquisitions(tmp_path, monkeypatch) == 1


@pytest.mark.asyncio
async def test_serialization_holds_on_a_second_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The same contention again, on the fresh loop pytest-asyncio gives each test.  A single
    # module-level asyncio.Lock binds to whichever loop first contends it and raises on every
    # other, so this is what stops the per-loop lock being "simplified" away.
    assert await _run_two_overlapping_acquisitions(tmp_path, monkeypatch) == 1


@pytest.mark.asyncio
async def test_a_failed_acquisition_releases_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An acquisition that raises (an unreachable CA, a broker 409) must not wedge every later one.
    config, dns_provider = _config(tmp_path), _dns(tmp_path)

    async def fail(**kwargs: Any) -> None:
        raise RuntimeError("CA unreachable")

    monkeypatch.setattr(provision, "acquire_tls_cert", fail)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="CA unreachable"):
            await acquire_cert_for_domain(
                config, "a.example.com", tmp_path / "c.crt", tmp_path / "c.key", None, dns_provider
            )

    assert not provision._issuance_lock().locked()


@pytest.mark.asyncio
async def test_an_instance_without_dns_is_refused_before_the_lock(tmp_path: Path) -> None:
    # DNS-01 is answered from a zone this instance serves; without one the CA would just time out.
    dns_provider = _dns(tmp_path, bind_ip=None)

    with pytest.raises(RuntimeError, match="CoreDNS must be enabled"):
        await acquire_cert_for_domain(
            _config(tmp_path), "a.example.com", tmp_path / "c.crt", tmp_path / "c.key", None, dns_provider
        )

    assert not provision._issuance_lock().locked()
