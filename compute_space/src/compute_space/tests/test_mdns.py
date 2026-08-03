from __future__ import annotations

import socket
import struct

from compute_space.core import mdns


class _FakeSocket:
    """Captures sendto() so _handle can be tested without real multicast."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
        self.sent.append((data, addr))
        return len(data)


def _responder(bases: tuple[str, ...], ip: str = "192.168.1.50") -> mdns.MdnsResponder:
    return mdns.MdnsResponder(sock=_FakeSocket(), lan_ip=ip, bases=bases)  # type: ignore[arg-type]


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
    assert mdns._parse_a_answers(packet) == ("192.168.1.50",)


def test_ignores_foreign_domain() -> None:
    r = _responder(("openhost.local",))
    r._handle(mdns._build_query("example.com"), ("192.168.1.9", 5353))
    assert r.sock.sent == []  # type: ignore[attr-defined]


def test_legacy_unicast_query_answered_to_sender() -> None:
    r = _responder(("openhost.local",))
    sender = ("192.168.1.9", 40000)  # ephemeral port -> legacy resolver
    r._handle(mdns._build_query("openhost.local"), sender)

    packet, addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert addr == sender
    # Legacy responses echo the question (qdcount=1) and don't set the cache-flush bit.
    _id, _flags, qdcount, ancount = struct.unpack("!HHHH", packet[:8])
    assert qdcount == 1 and ancount == 1


def test_qu_bit_gets_unicast_reply() -> None:
    r = _responder(("openhost.local",))
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    question = mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN | mdns._QU_BIT)
    sender = ("192.168.1.9", 5353)
    r._handle(header + question, sender)

    _packet, addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert addr == sender


def test_update_swaps_served_set() -> None:
    r = _responder(("openhost.local",))
    r.update(("newhost.local",), "10.0.0.2")
    assert r._owns("app.newhost.local")
    assert not r._owns("app.openhost.local")

    r._handle(mdns._build_query("app.newhost.local"), ("192.168.1.9", 5353))
    packet, _addr = r.sock.sent[0]  # type: ignore[attr-defined]
    assert mdns._parse_a_answers(packet) == ("10.0.0.2",)


def test_a_record_wire_shape() -> None:
    rr = mdns._a_record("openhost.local", "1.2.3.4", 120, cache_flush=True)
    name, offset = mdns._decode_name(rr, 0)
    rtype, rrclass, ttl, rdlen = struct.unpack("!HHIH", rr[offset : offset + 10])
    assert name == "openhost.local"
    assert rtype == mdns._TYPE_A
    assert rrclass == mdns._CLASS_IN | mdns._CACHE_FLUSH
    assert ttl == 120
    assert socket.inet_ntoa(rr[offset + 10 : offset + 10 + rdlen]) == "1.2.3.4"
