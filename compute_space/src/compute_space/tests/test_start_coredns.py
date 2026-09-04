from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.domains import Domain
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.tests.conftest import stub_coredns_spawn
from compute_space.web import start as start_mod

PUBLIC_IP = "203.0.113.10"


def _cfg(tmp_path: Path) -> DefaultConfig:
    return DefaultConfig(data_root_dir=str(tmp_path))


def test_hairpin_gateway_is_none_when_the_interface_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # openhost0 only exists on provisioned hosts; asking CoreDNS to bind an address that isn't
    # there stops it starting at all, so the view has to be dropped instead.
    monkeypatch.setattr(start_mod, "is_bindable", lambda ip: False)

    assert start_mod._hairpin_gateway_ip() is None


def test_hairpin_gateway_is_the_container_gateway_when_bindable(monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[str] = []
    monkeypatch.setattr(start_mod, "is_bindable", lambda ip: probed.append(ip) or True)

    assert start_mod._hairpin_gateway_ip() == CONTAINER_GATEWAY_IP
    assert probed == [CONTAINER_GATEWAY_IP]


def test_ensure_coredns_uses_path_binary_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_mod.shutil, "which", lambda name: "/usr/local/bin/coredns")
    installs: list[object] = []
    monkeypatch.setattr(start_mod, "install_pinned_binary", lambda *a, **k: installs.append(a))

    result = start_mod._ensure_coredns_binary(_cfg(tmp_path))

    assert result == "/usr/local/bin/coredns"
    assert installs == []  # provisioned binary on PATH -> no self-heal download


def test_ensure_coredns_self_heals_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_mod.shutil, "which", lambda name: None)
    installed: dict[str, object] = {}

    def fake_install(binary: object, dest: str) -> None:
        installed["binary"] = binary
        installed["dest"] = dest

    monkeypatch.setattr(start_mod, "install_pinned_binary", fake_install)

    cfg = _cfg(tmp_path)
    result = start_mod._ensure_coredns_binary(cfg)

    expected = str(cfg.openhost_data_path / "coredns")
    assert result == expected
    assert installed["dest"] == expected
    assert installed["binary"] == get_pinned_binary("coredns")


def test_an_unbindable_public_ip_names_the_address_not_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    # coredns_enabled with nothing to bind used to fall through to bind_ip=None, which disabled DNS
    # while every message downstream said CoreDNS was not enabled -- pointing at the one setting
    # the operator had in fact turned on.
    monkeypatch.setattr(start_mod, "infer_inbound_ipv4", lambda public_ip: None)
    config = DefaultConfig(data_root_dir="/tmp/unused", coredns_enabled=True, public_ip=PUBLIC_IP)

    with pytest.raises(RuntimeError, match="no local address to bind to"):
        start_mod._dns_bind_ip(config)


@pytest.mark.asyncio
async def test_dns_is_not_bound_when_coredns_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A public_ip with coredns off (local_http_only) still builds a provider and publishes the
    # router records into it; it just has no view to serve them on.
    monkeypatch.setattr(start_mod, "_hairpin_gateway_ip", lambda: None)
    config = DefaultConfig(data_root_dir=str(tmp_path), coredns_enabled=False, public_ip=PUBLIC_IP)
    config.make_all_dirs()

    dns_provider = await start_mod._start_dns(config, (Domain(name="host.example.com", tls=True),))

    assert dns_provider.bind_ip is None
    assert dns_provider.zones == ()
    assert not config.coredns_corefile_path.exists()


@pytest.mark.asyncio
async def test_the_zones_carry_the_router_addresses_from_the_moment_they_are_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Publishing after the first add_zone means CoreDNS starts on a zone holding only SOA and NS:
    # NODATA at the apex, NXDOMAIN for every `<app>.<domain>`, both negative-cached by resolvers
    # for the SOA minimum.  The zone file must already route the space when the process comes up.
    stub_coredns_spawn(monkeypatch)
    monkeypatch.setattr(start_mod, "infer_inbound_ipv4", lambda public_ip: "10.0.0.5")
    monkeypatch.setattr(start_mod, "_hairpin_gateway_ip", lambda: None)
    monkeypatch.setattr(start_mod, "_ensure_coredns_binary", lambda config: "coredns")

    config = DefaultConfig(data_root_dir=str(tmp_path), coredns_enabled=True, public_ip=PUBLIC_IP)
    config.make_all_dirs()

    zone_when_started: dict[str, str] = {}
    real_add_zone = start_mod.InternalDnsProvider.add_zone

    async def capture_add_zone(self: start_mod.InternalDnsProvider, zone: str) -> None:
        await real_add_zone(self, zone)
        zone_when_started[zone] = (config.zones_dir / f"{zone}.zone").read_text()

    monkeypatch.setattr(start_mod.InternalDnsProvider, "add_zone", capture_add_zone)

    dns_provider = await start_mod._start_dns(config, (Domain(name="host.example.com", tls=True),))
    await dns_provider.cleanup()

    served = zone_when_started["host.example.com"]
    assert f"@   300  IN A  {PUBLIC_IP}" in served
    assert f"*   300  IN A  {PUBLIC_IP}" in served
    assert f"ns   300  IN A  {PUBLIC_IP}" in served
