"""Unit tests for per-app egress profile parsing and address math."""

from __future__ import annotations

import os

import pytest

from compute_space.core import egress
from compute_space.core.manifest import parse_manifest_from_string


def _write_profile(profiles_dir: str, name: str, body: str) -> None:
    os.makedirs(profiles_dir, exist_ok=True)
    with open(os.path.join(profiles_dir, f"{name}.conf"), "w") as f:
        f.write(body)


WG_BASIC = """
[Interface]
PrivateKey = QOR34= 
Address = 10.77.0.2/24
DNS = 10.77.0.1

[Peer]
PublicKey = abc=
Endpoint = 1.2.3.4:51820
AllowedIPs = 0.0.0.0/0
"""


def test_load_profile_parses_address_and_dns(tmp_path):
    profiles = str(tmp_path / "egress_profiles")
    _write_profile(profiles, "home", WG_BASIC)
    prof = egress.load_profile(profiles, "home")
    assert prof.name == "home"
    assert prof.interface_address == "10.77.0.2/24"
    assert prof.dns_server == "10.77.0.1"
    assert prof.config_path.endswith("home.conf")


def test_load_profile_defaults_dns_when_absent(tmp_path):
    profiles = str(tmp_path / "egress_profiles")
    _write_profile(profiles, "vpn", "[Interface]\nAddress = 10.66.0.5/32\n")
    prof = egress.load_profile(profiles, "vpn")
    assert prof.dns_server == "1.1.1.1"


def test_load_profile_missing_raises(tmp_path):
    with pytest.raises(egress.EgressProfileError):
        egress.load_profile(str(tmp_path), "nope")


def test_load_profile_no_address_raises(tmp_path):
    profiles = str(tmp_path / "p")
    _write_profile(profiles, "bad", "[Interface]\nPrivateKey = x=\n")
    with pytest.raises(egress.EgressProfileError):
        egress.load_profile(profiles, "bad")


def test_load_profile_invalid_address_raises(tmp_path):
    profiles = str(tmp_path / "p")
    _write_profile(profiles, "bad", "[Interface]\nAddress = not-an-ip\n")
    with pytest.raises(egress.EgressProfileError):
        egress.load_profile(profiles, "bad")


def test_profile_exists(tmp_path):
    profiles = str(tmp_path / "p")
    _write_profile(profiles, "home", WG_BASIC)
    assert egress.profile_exists(profiles, "home")
    assert not egress.profile_exists(profiles, "away")


def test_ingress_ips_distinct_per_index():
    a = egress.ingress_ips_for_index(1)
    b = egress.ingress_ips_for_index(2)
    assert a == ("10.199.1.1", "10.199.1.2")
    assert b == ("10.199.2.1", "10.199.2.2")
    assert a != b


def test_ingress_ips_out_of_range():
    with pytest.raises(ValueError):
        egress.ingress_ips_for_index(256)


def test_address_only_takes_first_of_dual_stack(tmp_path):
    profiles = str(tmp_path / "p")
    _write_profile(profiles, "ds", "[Interface]\nAddress = 10.5.0.2/24, fd00::2/64\n")
    prof = egress.load_profile(profiles, "ds")
    assert prof.interface_address == "10.5.0.2/24"


# --- manifest-level egress validation ---

MANIFEST_BASE = """
[app]
name = "x"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""


def test_manifest_egress_parsed():
    m = parse_manifest_from_string(MANIFEST_BASE + '\negress = "home"\n')
    assert m.egress == "home"


def test_manifest_egress_default_empty():
    m = parse_manifest_from_string(MANIFEST_BASE)
    assert m.egress == ""


def test_manifest_egress_invalid_name():
    with pytest.raises(ValueError):
        parse_manifest_from_string(MANIFEST_BASE + '\negress = "Bad Name!"\n')


def test_manifest_egress_conflicts_with_network_host():
    body = MANIFEST_BASE + '\nnetwork_host = true\negress = "home"\n'
    with pytest.raises(ValueError):
        parse_manifest_from_string(body)
