from __future__ import annotations

import socket
import struct

import pytest

from compute_space.core import mdns
from compute_space.tests.conftest import fake_mdns_responder


def _responder(bases: tuple[str, ...], ip: str = "192.168.1.50", ip6: str | None = None) -> mdns.MdnsResponder:
    return fake_mdns_responder(bases, ip, ip6)


def _sent(r: mdns.MdnsResponder) -> list[tuple[bytes, tuple[object, ...]]]:
    return r.transports[0].sock.sent  # type: ignore[attr-defined]


def _handle(r: mdns.MdnsResponder, data: bytes, addr: tuple[object, ...]) -> None:
    r._handle(r.transports[0], data, addr)


def _question(name: str, qtype: int = mdns._TYPE_A, qclass: int = mdns._CLASS_IN) -> mdns._Question:
    return mdns._Question(name=name, qtype=qtype, qclass=qclass)


def _query(name: str, qtype: int = mdns._TYPE_A, ident: int = 0) -> bytes:
    header = struct.pack("!HHHHHH", ident, 0, 1, 0, 0, 0)
    return header + mdns._encode_name(name) + struct.pack("!HH", qtype, mdns._CLASS_IN)


def _records(data: bytes) -> list[tuple[str, int, bytes]]:
    """Every (name, rrtype, rdata) across a response's answer + additional sections."""
    _ident, _flags, qdcount, ancount, _ns, arcount = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        _name, offset = mdns._decode_name(data, offset)
        offset += 4
    out: list[tuple[str, int, bytes]] = []
    for _ in range(ancount + arcount):
        name, offset = mdns._decode_name(data, offset)
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        out.append((name, rtype, data[offset : offset + rdlen]))
        offset += rdlen
    return out


def test_name_roundtrip() -> None:
    name, offset = mdns._decode_name(mdns._encode_name("myapp.openhost.local"), 0)
    assert name == "myapp.openhost.local"
    assert offset == len(mdns._encode_name("myapp.openhost.local"))


def test_answers_owned_query_over_multicast() -> None:
    r = _responder(("openhost.local",))
    _handle(r, mdns._build_query("myapp.openhost.local"), ("192.168.1.9", 5353))

    sent = _sent(r)
    assert len(sent) == 1
    packet, addr = sent[0]
    assert addr == (mdns._MDNS_GROUP, mdns._MDNS_PORT)
    assert mdns._parse_a_answers(packet) == (("myapp.openhost.local", "192.168.1.50"),)


def test_aaaa_query_gets_nsec_denial_not_silence() -> None:
    # Staying silent on AAAA makes getaddrinfo (which asks A+AAAA and waits for both) stall until
    # its resolver times out — 5s per lookup, the whole `.local` slowness.
    r = _responder(("openhost.local",))
    _handle(r, _query("app.openhost.local", mdns._TYPE_AAAA), ("192.168.1.9", 5353))

    packet, _addr = _sent(r)[0]
    assert _records(packet) == [
        ("app.openhost.local", mdns._TYPE_NSEC, mdns._encode_name("app.openhost.local") + bytes([0, 1, 0x40]))
    ]


def test_a_answer_carries_nsec_so_parallel_aaaa_need_not_wait() -> None:
    r = _responder(("openhost.local",))
    _handle(r, _query("openhost.local"), ("192.168.1.9", 5353))

    packet, _addr = _sent(r)[0]
    types = [(name, rtype) for name, rtype, _rdata in _records(packet)]
    assert types == [("openhost.local", mdns._TYPE_A), ("openhost.local", mdns._TYPE_NSEC)]
    # The A record is the answer; the denial rides along in the additional section.
    assert struct.unpack("!HH", packet[6:10]) == (1, 0)  # ancount=1, nscount=0
    assert struct.unpack("!H", packet[10:12])[0] == 1  # arcount=1


def test_a_and_aaaa_in_one_query_answers_each_once() -> None:
    # getaddrinfo may pack both questions into one message; the name must not be denied and
    # answered at the same time.
    r = _responder(("openhost.local",))
    header = struct.pack("!HHHHHH", 0, 0, 2, 0, 0, 0)
    body = mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN)
    body += mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_AAAA, mdns._CLASS_IN)
    _handle(r, header + body, ("192.168.1.9", 5353))

    packet, _addr = _sent(r)[0]
    assert [(n, t) for n, t, _rd in _records(packet)] == [
        ("openhost.local", mdns._TYPE_A),
        ("openhost.local", mdns._TYPE_NSEC),
    ]


def test_serves_aaaa_when_the_box_has_an_address() -> None:
    r = _responder(("openhost.local",), ip6="fd00::5")
    _handle(r, _query("openhost.local", mdns._TYPE_AAAA), ("192.168.1.9", 5353))

    packet, _addr = _sent(r)[0]
    records = _records(packet)
    assert (records[0][0], records[0][1]) == ("openhost.local", mdns._TYPE_AAAA)
    assert socket.inet_ntop(socket.AF_INET6, records[0][2]) == "fd00::5"
    # The A rides along, so a client asking both is satisfied by this one packet.
    assert [(n, t) for n, t, _rd in records[1:]] == [
        ("openhost.local", mdns._TYPE_A),
        ("openhost.local", mdns._TYPE_NSEC),
    ]


def test_nsec_bitmap_admits_aaaa_once_it_is_served() -> None:
    # The bitmap is a positive denial of every type omitted, and clients cache it — so it has to
    # grow the moment we start serving AAAA, or they'd ignore the records we just added.
    a_only = mdns._nsec_record("openhost.local", (mdns._TYPE_A,), 120, cache_flush=True)
    both = mdns._nsec_record("openhost.local", (mdns._TYPE_A, mdns._TYPE_AAAA), 120, cache_flush=True)
    suffix = len(mdns._encode_name("openhost.local"))
    assert a_only[-(suffix + 3) :][suffix:] == bytes([0, 1, 0x40])  # window 0, 1 byte, bit 1 (A)
    assert both[-(suffix + 6) :][suffix:] == bytes([0, 4, 0x40, 0x00, 0x00, 0x08])  # + bit 28 (AAAA)


def test_ipv6_lan_peer_with_a_global_address_is_answered() -> None:
    # v6 LAN clients commonly hold a *global* SLAAC address, so `is_private` alone would drop them;
    # sharing our /64 is what makes them local.
    assert mdns._is_local_source("2a00:1450:4001:1::99", "2a00:1450:4001:1::1")
    assert not mdns._is_local_source("2a00:1450:9999:1::99", "2a00:1450:4001:1::1")  # different /64
    assert not mdns._is_local_source("2a00:1450:4001:1::99", None)  # no v6 of ours to compare against
    assert mdns._is_local_source("fe80::99", None)  # link-local is unambiguously on-link
    assert mdns._is_local_source("fd00::99", None)  # ULA


def test_narrower_prefix_rejects_peer_outside_it() -> None:
    # A cloud VM interface often carries a bare /128 (the rest of the subnet is routed off-link, not
    # on-link), unlike a home/corporate LAN's /64 — so the check must honor whatever prefix the
    # caller passes rather than always assuming /64.
    same_64 = ("2a00:1450:4001:1::99", "2a00:1450:4001:1::1")
    assert mdns._is_local_source(*same_64, our_ip6_prefix=64)
    assert not mdns._is_local_source(*same_64, our_ip6_prefix=128)  # only an exact match on /128


def test_interface_prefix_len_defaults_to_64_when_unknown() -> None:
    # No such address is configured on this machine (test or CI), so this exercises the fallback
    # every non-Linux dev machine takes too — /proc/net/if_inet6 not existing must not crash it.
    assert mdns._interface_prefix_len("2001:db8::dead:beef") == 64


def test_interface_ipv4_prefix_len_defaults_to_none_when_unknown() -> None:
    # TEST-NET-3 (RFC 5737): never a real connected route on a test or CI machine, and exercises the
    # fallback every non-Linux dev machine takes too — /proc/net/route not existing must not crash it.
    assert mdns._interface_ipv4_prefix_len("203.0.113.1") is None


def test_ipv4_peer_outside_our_actual_subnet_is_rejected_when_known() -> None:
    # Mirrors the v6 /64 case: a private v4 sender must be rejected once we know our *actual* subnet,
    # even though `is_private` alone would have accepted it — it could be an unrelated RFC 1918 range
    # elsewhere on a routed network, a VPN client, a Docker bridge, and so on.
    assert mdns._is_local_source("192.168.1.9", our_ip="192.168.1.50", our_ip_prefix=24)
    assert not mdns._is_local_source("192.168.2.9", our_ip="192.168.1.50", our_ip_prefix=24)
    # Without a discovered subnet (the common case), keep the old broad "any private sender" behavior.
    assert mdns._is_local_source("10.99.99.99")


def test_ignores_foreign_domain() -> None:
    r = _responder(("openhost.local",))
    _handle(r, mdns._build_query("example.com"), ("192.168.1.9", 5353))
    assert _sent(r) == []


def test_legacy_unicast_query_answered_to_sender() -> None:
    r = _responder(("openhost.local",))
    sender = ("192.168.1.9", 40000)  # ephemeral port -> legacy resolver
    query = struct.pack("!HHHHHH", 0xBEEF, 0, 1, 0, 0, 0)
    query += mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN)
    _handle(r, query, sender)

    packet, addr = _sent(r)[0]
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
    _handle(r, query, ("192.168.1.9", 5353))

    packet, _addr = _sent(r)[0]
    assert struct.unpack("!H", packet[:2])[0] == 0


def test_qu_bit_gets_unicast_reply() -> None:
    r = _responder(("openhost.local",))
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    question = mdns._encode_name("openhost.local") + struct.pack("!HH", mdns._TYPE_A, mdns._CLASS_IN | mdns._QU_BIT)
    sender = ("192.168.1.9", 5353)
    _handle(r, header + question, sender)

    _packet, addr = _sent(r)[0]
    assert addr == sender


def test_update_swaps_served_set() -> None:
    r = _responder(("openhost.local",), "10.0.0.2")
    r.update(("newhost.local",))
    assert r._owns("app.newhost.local")
    assert not r._owns("app.openhost.local")

    _handle(r, mdns._build_query("app.newhost.local"), ("192.168.1.9", 5353))
    packet, _addr = _sent(r)[0]
    assert mdns._parse_a_answers(packet) == (("app.newhost.local", "10.0.0.2"),)


def test_drops_query_from_public_source() -> None:
    r = _responder(("openhost.local",))
    _handle(r, mdns._build_query("openhost.local"), ("8.8.8.8", 5353))  # routed, off-LAN
    assert _sent(r) == []


def test_answers_query_from_private_source() -> None:
    assert mdns._is_local_source("192.168.1.9")
    assert mdns._is_local_source("10.1.2.3")
    assert not mdns._is_local_source("8.8.8.8")


def test_ensure_starts_then_stops_responder(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _responder(("openhost.local",))
    monkeypatch.setattr(mdns, "default_route_source_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(mdns, "start_mdns", lambda bases, lan_ip, lan_ip6=None: fake)
    mdns.set_active_mdns(None)

    # First `.local` domain appears → responder starts.
    monkeypatch.setattr(mdns, "mdns_bases", lambda db: ("openhost.local",))
    mdns.ensure_mdns_for_domains(db=None)  # type: ignore[arg-type]
    assert mdns.get_active_mdns() is fake

    # Last `.local` domain removed → responder stops and deregisters.
    monkeypatch.setattr(mdns, "mdns_bases", lambda db: ())
    mdns.ensure_mdns_for_domains(db=None)  # type: ignore[arg-type]
    assert mdns.get_active_mdns() is None
    assert fake.transports[0].sock.closed  # type: ignore[attr-defined]


def test_ensure_rebinds_when_lan_ip_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _responder(("openhost.local",))
    second = _responder(("openhost.local",), "192.168.1.60")
    started: list[str] = []

    def _start(bases: tuple[str, ...], lan_ip: str, lan_ip6: str | None = None) -> mdns.MdnsResponder:
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
    assert first.transports[0].sock.closed  # type: ignore[attr-defined]
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

    def __init__(self, responses: list[bytes], repeat: bool = False, addr: tuple[str, int] = ("192.168.1.9", 5353)) -> None:
        self._responses = responses
        self._repeat = repeat
        self._addr = addr

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendto(self, data: bytes, _addr: tuple[str, int]) -> int:
        return len(data)

    def recvfrom(self, _bufsize: int) -> tuple[bytes, tuple[str, int]]:
        if self._repeat:
            return self._responses[0], self._addr
        if self._responses:
            return self._responses.pop(0), self._addr
        raise TimeoutError


def _a_response(name: str, ip: str) -> bytes:
    return mdns._build_response((_question(name),), ip, None, legacy=False, ident=0)


def test_probe_ignores_unrelated_a_record() -> None:
    sock = _ProbeSocket([_a_response("printer.local", "192.168.1.99")])
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") is None  # type: ignore[arg-type]


def test_probe_detects_matching_a_record() -> None:
    sock = _ProbeSocket([_a_response("OpenHost.local.", "192.168.1.77")])
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") == "192.168.1.77"  # type: ignore[arg-type]


def test_probe_ignores_off_link_sender() -> None:
    # The probe answers _handle's own trust boundary: a spoofed/off-link response injected during
    # the startup conflict window must not be able to produce a bogus "already claimed" warning.
    sock = _ProbeSocket([_a_response("openhost.local", "192.168.1.77")], addr=("8.8.8.8", 5353))
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50") is None  # type: ignore[arg-type]


def test_probe_ignores_sender_outside_known_subnet() -> None:
    sock = _ProbeSocket([_a_response("openhost.local", "192.168.2.77")], addr=("192.168.2.9", 5353))
    assert mdns._probe_conflict(sock, "openhost.local", "192.168.1.50", our_ip_prefix=24) is None  # type: ignore[arg-type]


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
