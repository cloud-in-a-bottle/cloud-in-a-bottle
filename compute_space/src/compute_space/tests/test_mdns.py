from __future__ import annotations

import socket
import struct

import pytest

from compute_space.core import mdns
from compute_space.tests.conftest import FakeMdnsSocket


def _responder(bases: tuple[str, ...], ip: str = "192.168.1.50") -> mdns.MdnsResponder:
    return mdns.MdnsResponder(sock=FakeMdnsSocket(), lan_ip=ip, bases=bases)  # type: ignore[arg-type]


def test_name_roundtrip() -> None:
    name, offset = mdns._decode_name(mdns._encode_name("myapp.openhost.local"), 0)
    assert name == "myapp.openhost.local"
    assert offset == len(mdns._encode_name("myapp.openhost.local"))


def test_answers_owned_query_over_multicast() -> None:
    r = _responder(("openhost.local",))
    r._handle(mdns._build_query("myapp.openhost.local"), ("192.168.1.9", 5353))

    sent = r.sock.sent  # type: ignore[attr-defined]
    assert len(sent) == 1
    packet, addr = sent[0]
    assert addr == (mdns._MDNS_GROUP, mdns._MDNS_PORT)
    assert mdns._parse_a_answers(packet) == (("myapp.openhost.local", "192.168.1.50"),)


def test_ignores_foreign_domain() -> None:
    r = _responder(("openhost.local",))
    r._handle(mdns._build_query("example.com"), ("192.168.1.9", 5353))
    assert r.sock.sent == []  # type: ignore[attr-defined]


def test_legacy_unicast_query_answered_to_sender() -> None:
    r = _responder(("openhost.local",))
    sender = ("192.168.1.9", 40000)  # ephemeral port -> legacy resolver
    query = struct.pack("!HHHHHH", 0xBEEF, 0, 1, 0, 0, 0)
    query += mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN)
    r._handle(query, sender)

    packet, addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert addr == sender
    # Legacy responses echo the query's transaction ID (a stub resolver drops any other) and the
    # question (qdcount=1), and don't set the cache-flush bit.
    ident, _flags, qdcount, ancount = struct.unpack("!HHHH", packet[:8])
    assert ident == 0xBEEF
    assert qdcount == 1 and ancount == 1


def test_multicast_response_zeroes_transaction_id() -> None:
    # RFC 6762 §18.1: a multicast response must carry ID 0, whatever the query used.
    r = _responder(("openhost.local",))
    query = struct.pack("!HHHHHH", 0xBEEF, 0, 1, 0, 0, 0)
    query += mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN)
    r._handle(query, ("192.168.1.9", 5353))

    packet, _addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert struct.unpack("!H", packet[:2])[0] == 0


def test_qu_bit_gets_unicast_reply() -> None:
    r = _responder(("openhost.local",))
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    question = mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN | mdns._QU_BIT)
    sender = ("192.168.1.9", 5353)
    r._handle(header + question, sender)

    _packet, addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert addr == sender


def test_update_swaps_served_set() -> None:
    r = _responder(("openhost.local",), "10.0.0.2")
    r.update(("newhost.local",))
    assert r._owns("app.newhost.local")
    assert not r._owns("app.openhost.local")

    r._handle(mdns._build_query("app.newhost.local"), ("192.168.1.9", 5353))
    packet, _addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert mdns._parse_a_answers(packet) == (("app.newhost.local", "10.0.0.2"),)


def test_drops_query_from_public_source() -> None:
    r = _responder(("openhost.local",))
    r._handle(mdns._build_query("openhost.local"), ("8.8.8.8", 5353))  # routed, off-LAN
    assert r.sock.sent == []  # type: ignore[attr-defined]


def test_answers_query_from_private_source() -> None:
    assert mdns._is_local_source("192.168.1.9")
    assert mdns._is_local_source("10.1.2.3")
    assert not mdns._is_local_source("8.8.8.8")


def test_ensure_starts_then_stops_responder(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _responder(("openhost.local",))
    monkeypatch.setattr(mdns, "default_route_source_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(mdns, "start_mdns", lambda bases, lan_ip: fake)
    mdns.set_active_mdns(None)

    # First `.local` domain appears → responder starts.
    monkeypatch.setattr(mdns, "mdns_bases", lambda db: ("openhost.local",))
    mdns.ensure_mdns_for_domains(db=None)  # type: ignore[arg-type]
    assert mdns.get_active_mdns() is fake

    # Last `.local` domain removed → responder stops and deregisters.
    monkeypatch.setattr(mdns, "mdns_bases", lambda db: ())
    mdns.ensure_mdns_for_domains(db=None)  # type: ignore[arg-type]
    assert mdns.get_active_mdns() is None
    assert fake.sock.closed  # type: ignore[attr-defined]


def test_ensure_rebinds_when_lan_ip_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _responder(("openhost.local",))
    second = _responder(("openhost.local",), "192.168.1.60")
    started: list[str] = []

    def _start(bases: tuple[str, ...], lan_ip: str) -> mdns.MdnsResponder:
        started.append(lan_ip)
        return second if started[1:] else first

    monkeypatch.setattr(mdns, "start_mdns", _start)
    monkeypatch.setattr(mdns, "mdns_bases", lambda db: ("openhost.local",))
    monkeypatch.setattr(mdns, "default_route_source_ip", lambda: "192.168.1.50")
    mdns.set_active_mdns(None)
    mdns.ensure_mdns_for_domains(db=None)  # type: ignore[arg-type]

    # A DHCP renewal moves the address; the socket's group membership is pinned to the interface it
    # was opened on, so the responder must be rebound rather than just re-pointed.
    monkeypatch.setattr(mdns, "default_route_source_ip", lambda: "192.168.1.60")
    mdns.ensure_mdns_for_domains(db=None)  # type: ignore[arg-type]

    assert started == ["192.168.1.50", "192.168.1.60"]
    assert first.sock.closed  # type: ignore[attr-defined]
    assert mdns.get_active_mdns() is second


def test_self_referencing_pointer_raises() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        mdns._decode_name(struct.pack("!H", 0xC000), 0)  # pointer -> offset 0 (itself)


def test_mutually_referencing_pointers_raise() -> None:
    data = struct.pack("!H", 0xC002) + struct.pack("!H", 0xC000)  # 0 -> 2, 2 -> 0
    with pytest.raises(ValueError):  # noqa: PT011
        mdns._decode_name(data, 0)


def test_decode_name_follows_backward_pointer() -> None:
    prefix = mdns._encode_name("openhost.local")  # at offset 0
    data = prefix + bytes([3]) + b"app" + struct.pack("!H", 0xC000)
    name, offset = mdns._decode_name(data, len(prefix))
    assert name == "app.openhost.local"
    assert offset == len(data)


class _ProbeSocket:
    """Feeds _probe_conflict crafted responses, then times out (or repeats forever)."""

    def __init__(self, responses: list[bytes], repeat: bool = False) -> None:
        self._responses = responses
        self._repeat = repeat

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendto(self, data: bytes, _addr: tuple[str, int]) -> int:
        return len(data)

    def recvfrom(self, _bufsize: int) -> tuple[bytes, tuple[str, int]]:
        if self._repeat:
            return self._responses[0], ("192.168.1.9", 5353)
        if self._responses:
            return self._responses.pop(0), ("192.168.1.9", 5353)
        raise TimeoutError


def _a_response(name: str, ip: str) -> bytes:
    return mdns._build_response((name,), ip, (), legacy=False, ident=0)


def test_probe_ignores_unrelated_a_record() -> None:
    sock = _ProbeSocket([_a_response("printer.local", "192.168.1.99")])
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") is None  # type: ignore[arg-type]


def test_probe_detects_matching_a_record() -> None:
    sock = _ProbeSocket([_a_response("OpenHost.local.", "192.168.1.77")])
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") == "192.168.1.77"  # type: ignore[arg-type]


def test_probe_skips_malformed_then_matches() -> None:
    cyclic = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + struct.pack("!H", 0xC00C)  # answer name -> itself
    sock = _ProbeSocket([cyclic, _a_response("openhost.local", "192.168.1.77")])
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") == "192.168.1.77"  # type: ignore[arg-type]


def test_probe_gives_up_on_endless_unrelated_chatter(monkeypatch: pytest.MonkeyPatch) -> None:
    # A busy LAN delivers unrelated packets faster than the per-recv timeout; only the overall
    # deadline ends the loop, and boot + /api/domains block on it.
    monkeypatch.setattr(mdns, "_PROBE_BUDGET", 0.05)
    sock = _ProbeSocket([_a_response("printer.local", "192.168.1.99")], repeat=True)
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") is None  # type: ignore[arg-type]


def test_a_record_wire_shape() -> None:
    rr = mdns._a_record("openhost.local", "1.2.3.4", 120, cache_flush=True)
    name, offset = mdns._decode_name(rr, 0)
    rtype, rrclass, ttl, rdlen = struct.unpack("!HHIH", rr[offset : offset + 10])
    assert name == "openhost.local"
    assert rtype == mdns._TYPE_A
    assert rrclass == mdns._CLASS_IN | mdns._CACHE_FLUSH
    assert ttl == 120
    assert socket.inet_ntoa(rr[offset + 10 : offset + 10 + rdlen]) == "1.2.3.4"
