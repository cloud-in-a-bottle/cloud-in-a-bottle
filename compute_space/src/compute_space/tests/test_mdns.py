from __future__ import annotations

import asyncio
import socket
import struct
from typing import Any

import pytest

from compute_space.core.domains import Domain
from compute_space.core.mdns import responder as responder_module
from compute_space.core.mdns.answers import MDNS_PORT
from compute_space.core.mdns.answers import build_answer
from compute_space.core.mdns.packets import TYPE_A
from compute_space.core.mdns.packets import TYPE_AAAA
from compute_space.core.mdns.packets import TYPE_NSEC
from compute_space.core.mdns.packets import MalformedPacket
from compute_space.core.mdns.packets import Query
from compute_space.core.mdns.packets import Question
from compute_space.core.mdns.packets import _read_name
from compute_space.core.mdns.packets import parse_query
from compute_space.core.mdns.responder import MdnsResponder

DOMAINS = ("myhost.local",)
IP = "192.168.1.50"


def _query_packet(*questions: tuple[str, int], query_id: int = 0) -> bytes:
    body = b""
    for name, qtype in questions:
        body += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
        body += struct.pack("!HH", qtype, 1)
    return struct.pack("!HHHHHH", query_id, 0, len(questions), 0, 0, 0) + body


def _records(packet: bytes) -> list[tuple[str, int, int, int, bytes]]:
    """Every (name, type, class, ttl, rdata) in the answer + additional sections of a response."""
    _, _, qd, an, ns, ar = struct.unpack_from("!HHHHHH", packet)
    offset = 12
    for _ in range(qd):
        _, offset = _read_name(packet, offset)
        offset += 4
    out = []
    for _ in range(an + ns + ar):
        name, offset = _read_name(packet, offset)
        rtype, rclass, ttl, rdlen = struct.unpack_from("!HHIH", packet, offset)
        offset += 10
        out.append((name, rtype, rclass, ttl, packet[offset : offset + rdlen]))
        offset += rdlen
    return out


def _answer(*questions: tuple[str, int], port: int = MDNS_PORT) -> bytes | None:
    query = parse_query(_query_packet(*questions, query_id=7))
    assert query is not None
    return build_answer(query, port, DOMAINS, IP)


def test_parse_query_follows_compression_pointers() -> None:
    # second question is "foo" + pointer to "myhost.local" in the first
    first = b"\x06myhost\x05local\x00" + struct.pack("!HH", TYPE_A, 1)
    second = b"\x03foo\xc0\x0c" + struct.pack("!HH", TYPE_AAAA, 1)
    packet = struct.pack("!HHHHHH", 0, 0, 2, 0, 0, 0) + first + second
    assert parse_query(packet) == Query(
        id=0, questions=(Question("myhost.local", TYPE_A), Question("foo.myhost.local", TYPE_AAAA))
    )


def test_parse_query_ignores_responses_and_rejects_garbage() -> None:
    assert parse_query(struct.pack("!HHHHHH", 0, 0x8400, 0, 0, 0, 0)) is None
    with pytest.raises(MalformedPacket):
        parse_query(b"\x00\x01")
    with pytest.raises(MalformedPacket):
        parse_query(struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + b"\xc0\x0c")  # pointer to itself


def test_parse_query_rejects_reserved_label_types_and_non_utf8() -> None:
    # A 0x80-prefixed "length" would otherwise read as a 191-byte label.
    reserved = b"\xbf" + b"\xff" * 191 + b"\x06myhost\x05local\x00" + struct.pack("!HH", TYPE_A, 1)
    with pytest.raises(MalformedPacket):
        parse_query(struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + reserved)
    not_utf8 = b"\x02\xff\xfe\x06myhost\x05local\x00" + struct.pack("!HH", TYPE_A, 1)
    with pytest.raises(MalformedPacket):
        parse_query(struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + not_utf8)


def test_any_app_subdomain_gets_an_a_record() -> None:
    reply = _answer(("myapp.myhost.local", TYPE_A))
    assert reply is not None
    a, nsec = _records(reply)
    assert a[:2] == ("myapp.myhost.local", TYPE_A)
    assert socket.inet_ntoa(a[4]) == IP
    assert a[2] & 0x8000  # cache-flush on multicast replies
    assert nsec[:2] == ("myapp.myhost.local", TYPE_NSEC)


def test_aaaa_gets_a_negative_answer() -> None:
    reply = _answer(("myapp.myhost.local", TYPE_AAAA))
    assert reply is not None
    [(name, rtype, _, _, _)] = _records(reply)
    assert (name, rtype) == ("myapp.myhost.local", TYPE_NSEC)


def test_other_names_are_ignored() -> None:
    assert _answer(("otherhost.local", TYPE_A)) is None
    assert _answer(("myhost.local.evil", TYPE_A)) is None
    assert _answer(("notmyhost.local", TYPE_A)) is None


def test_bare_domain_is_left_to_avahi() -> None:
    assert _answer(("myhost.local", TYPE_A)) is None
    assert _answer(("myhost.local", TYPE_AAAA)) is None


def test_legacy_unicast_reply_echoes_id_and_question_with_short_ttl() -> None:
    reply = _answer(("myapp.myhost.local", TYPE_A), port=40000)
    assert reply is not None
    query_id, _, qdcount, _, _, _ = struct.unpack_from("!HHHHHH", reply)
    assert (query_id, qdcount) == (7, 1)
    a = _records(reply)[0]
    assert a[2] == 1  # no cache-flush bit
    assert a[3] == 10


@pytest.mark.asyncio
async def test_responder_answers_over_the_wire() -> None:
    responder = MdnsResponder(port=0)
    await responder.update((Domain("example.com", tls=True), Domain("myhost.local")))
    assert responder._sock is not None
    port = responder._sock.getsockname()[1]
    loop = asyncio.get_running_loop()
    query = _query_packet(("myapp.myhost.local", TYPE_A), query_id=42)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setblocking(False)
        # Sent to the group over loopback, like a (legacy, so unicast-answered) querier on the LAN.
        client.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        await loop.sock_sendto(client, query, ("224.0.0.251", port))
        reply = await asyncio.wait_for(loop.sock_recv(client, 9000), timeout=5)
        # The socket is bound to the group, so unicast straight to it is never seen.
        await loop.sock_sendto(client, query, ("127.0.0.1", port))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.sock_recv(client, 9000), timeout=0.5)
    a = _records(reply)[0]
    # answered with the address the query arrived on
    assert socket.inet_ntoa(a[4]) == "127.0.0.1"

    await responder.update((Domain("example.com", tls=True),))
    assert responder._sock is None


async def _query_over_loopback(port: int, name: str, timeout: float = 5) -> bytes:
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setblocking(False)
        client.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        await loop.sock_sendto(client, _query_packet((name, TYPE_A)), ("224.0.0.251", port))
        return await asyncio.wait_for(loop.sock_recv(client, 9000), timeout=timeout)


@pytest.mark.asyncio
async def test_a_packet_that_raises_is_skipped_and_the_next_one_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    responder = MdnsResponder(port=0)
    await responder.update((Domain("myhost.local"),))
    assert responder._sock is not None
    port = responder._sock.getsockname()[1]
    real_build_answer = build_answer
    calls = 0

    def flaky_build_answer(*args: Any) -> bytes | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("boom")
        return real_build_answer(*args)

    monkeypatch.setattr(responder_module, "build_answer", flaky_build_answer)
    with pytest.raises(TimeoutError):
        await _query_over_loopback(port, "first.myhost.local", timeout=0.5)
    reply = await _query_over_loopback(port, "second.myhost.local")
    assert _records(reply)[0][0] == "second.myhost.local"
    await responder.stop()


@pytest.mark.asyncio
async def test_failing_to_listen_is_logged_not_raised_and_retried() -> None:
    # A socket on the same port without SO_REUSEADDR keeps the responder from binding.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("", 0))
        port = blocker.getsockname()[1]
        responder = MdnsResponder(port=port)
        await responder.update((Domain("myhost.local"),))
        assert responder._sock is None
    await responder.update((Domain("myhost.local"),))
    assert responder._sock is not None
    await responder.stop()
