"""Runtime public IP: detection guardrails, storage precedence, and the dynamic-DNS update."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from compute_space.config import DefaultConfig
from compute_space.core.dns import dynamic
from compute_space.core.dns import public_ip as public_ip_mod
from compute_space.core.dns import zonefile
from compute_space.core.dns.public_ip import detect_public_ip
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import is_public_ipv4
from compute_space.core.dns.public_ip import seed_public_ip
from compute_space.core.dns.public_ip import store_public_ip
from compute_space.core.dns.records import DnsRecord
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.db import init_db
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config

ZONE = "host.example.com"

# Detection tests need addresses that pass is_public_ipv4, and the RFC 5737 documentation ranges
# (203.0.113.0/24 and friends) do not: they are in the IANA special-purpose registry, so
# is_global rejects them exactly as it rejects RFC 1918. Nothing here makes a real request.
PUBLIC_A = "51.75.10.1"
PUBLIC_B = "51.75.20.2"
PUBLIC_C = "51.75.30.3"


def _space(tmp_path: Path, public_ip: str | None = "203.0.113.10") -> DefaultConfig:
    if public_ip is None:
        # No IP means no zone files to seed; the test only cares about storage precedence.
        config = DefaultConfig(data_root_dir=str(tmp_path), public_ip=None)
        config.make_all_dirs()
        init_db(config.db_path)
        with closing(open_db(config)) as db:
            seed_domains(db, Domain(ZONE, tls=True), [])
        return config
    return seeded_dns_config(tmp_path, Domain(ZONE, tls=True), public_ip=public_ip)


def _zone_records(config: DefaultConfig, name: str, rrtype: str) -> list[str]:
    found = zonefile.read_records(config.coredns_zonefile_path, ZONE)
    return [r.data for r in found if r.name == name and r.type == rrtype and r.data]


def _sources(answers: dict[str, str | Exception]) -> Any:
    """A mock transport returning a canned answer (or failure) per echo service."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers[str(request.url)]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, text=answer)

    return httpx.MockTransport(handler)


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(answers: dict[str, str | Exception]) -> None:
        transport = _sources(answers)
        real_client = httpx.Client

        def factory(*args: object, **kwargs: object) -> httpx.Client:
            kwargs.pop("follow_redirects", None)
            kwargs.pop("timeout", None)
            return real_client(transport=transport)

        monkeypatch.setattr(public_ip_mod.httpx, "Client", factory)

    return install


# ─── what counts as a usable address ───


@pytest.mark.parametrize(
    "candidate,expected",
    [
        (PUBLIC_A, True),
        ("10.0.0.5", False),  # private
        ("127.0.0.1", False),  # loopback
        ("169.254.1.1", False),  # link-local
        ("100.64.0.1", False),  # carrier-grade NAT
        ("203.0.113.10", False),  # documentation range, not routable
        ("::1", False),  # not IPv4
        ("not-an-ip", False),
        ("", False),
    ],
)
def test_only_a_routable_ipv4_is_usable(candidate: str, expected: bool) -> None:
    # Publishing a private or loopback address as the space's A record is always a mistake, and
    # a proxy or captive portal answering the echo service is exactly how that happens.
    assert is_public_ipv4(candidate) is expected


# ─── detection guardrails ───


def test_detection_requires_two_sources_to_agree(patched_client: Any) -> None:
    patched_client({url: PUBLIC_A for url in public_ip_mod._ECHO_SERVICES})
    assert detect_public_ip() == PUBLIC_A


def test_a_lone_dissenting_source_cannot_move_the_records(patched_client: Any) -> None:
    # A single flaky or hijacked echo service must not be able to point the space at an attacker.
    patched_client(
        {
            "https://api.ipify.org": PUBLIC_A,
            "https://ifconfig.me/ip": PUBLIC_B,
            "https://icanhazip.com": PUBLIC_C,
            "https://ipv4.icanhazip.com": "  ",
        }
    )
    assert detect_public_ip() is None


def test_detection_gives_up_rather_than_guessing_when_nothing_answers(patched_client: Any) -> None:
    patched_client({url: httpx.ConnectError("down") for url in public_ip_mod._ECHO_SERVICES})
    assert detect_public_ip() is None


def test_a_private_answer_does_not_count_toward_agreement(patched_client: Any) -> None:
    patched_client(
        {
            "https://api.ipify.org": "10.0.0.5",
            "https://ifconfig.me/ip": "10.0.0.5",
            "https://icanhazip.com": "192.168.1.1",
            "https://ipv4.icanhazip.com": "10.0.0.5",
        }
    )
    assert detect_public_ip() is None


# ─── storage precedence ───


def test_the_config_value_seeds_the_db_once(tmp_path: Path) -> None:
    config = _space(tmp_path)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert effective_public_ip(config, db) == "203.0.113.10"


def test_a_stale_config_cannot_undo_a_dynamic_update(tmp_path: Path) -> None:
    # The machine moved and the DB knows; a config.toml that was written before the move must not
    # win on the next restart.
    config = _space(tmp_path)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        store_public_ip(db, "198.51.100.7")
        seed_public_ip(config, db)  # as a restart would
        assert effective_public_ip(config, db) == "198.51.100.7"


def test_with_no_stored_or_configured_ip_there_is_none(tmp_path: Path) -> None:
    config = _space(tmp_path, public_ip=None)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert effective_public_ip(config, db) is None


# ─── the update itself ───


def test_an_ip_change_rewrites_the_router_owned_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _space(tmp_path)
    monkeypatch.setattr(dynamic, "detect_public_ip", lambda: "198.51.100.7")
    monkeypatch.setattr(dynamic, "reload_coredns_for_domains", lambda *a: True, raising=False)

    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert dynamic.check_once(config, db) == "198.51.100.7"

        assert effective_public_ip(config, db) == "198.51.100.7"
    for name in ("@", "*", "ns"):
        assert _zone_records(config, name, "A") == ["198.51.100.7"]


def test_an_unchanged_ip_is_not_a_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _space(tmp_path)
    monkeypatch.setattr(dynamic, "detect_public_ip", lambda: "203.0.113.10")
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert dynamic.check_once(config, db) is None


def test_a_detection_failure_leaves_the_records_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stale records beat pointing the space at nothing.
    config = _space(tmp_path)
    monkeypatch.setattr(dynamic, "detect_public_ip", lambda: None)
    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        assert dynamic.check_once(config, db) is None
        assert effective_public_ip(config, db) == "203.0.113.10"
    assert _zone_records(config, "*", "A") == ["203.0.113.10"]


def test_the_update_does_not_disturb_records_apps_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _space(tmp_path)
    monkeypatch.setattr(dynamic, "detect_public_ip", lambda: "198.51.100.7")
    monkeypatch.setattr(dynamic, "reload_coredns_for_domains", lambda *a: True, raising=False)

    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        zonefile.append_records(config.coredns_zonefile_path, ZONE, [DnsRecord("www", "A", 300, "192.0.2.50")])

        dynamic.check_once(config, db)

    assert _zone_records(config, "www", "A") == ["192.0.2.50"]


def test_dynamic_records_carry_a_short_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pointless to poll every few minutes if resolvers cache the old address for an hour.
    config = _space(tmp_path)
    monkeypatch.setattr(dynamic, "detect_public_ip", lambda: "198.51.100.7")
    monkeypatch.setattr(dynamic, "reload_coredns_for_domains", lambda *a: True, raising=False)

    with closing(open_db(config)) as db:
        seed_public_ip(config, db)
        dynamic.check_once(config, db)
    records = zonefile.read_records(config.coredns_zonefile_path, ZONE)
    assert [r.ttl for r in records if r.name == "*" and r.type == "A"] == [60]
