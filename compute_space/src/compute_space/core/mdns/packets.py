"""Just enough of the DNS wire format (RFC 1035) to answer mDNS (RFC 6762) A queries."""

from __future__ import annotations

import socket
import struct

import attr

TYPE_A = 1
TYPE_AAAA = 28
TYPE_NSEC = 47
TYPE_ANY = 255
_CLASS_IN = 1
# Top bit of a record's class: tells peers to replace (not add to) what they have cached for the name.
_CACHE_FLUSH = 0x8000
_FLAG_RESPONSE = 0x8000
_FLAG_AUTHORITATIVE = 0x0400
_OPCODE_MASK = 0x7800

_HEADER = struct.Struct("!HHHHHH")  # id, flags, qdcount, ancount, nscount, arcount
_TYPE_CLASS = struct.Struct("!HH")
_RR_FIXED = struct.Struct("!HHIH")  # type, class, ttl, rdlength


class MalformedPacket(Exception):
    pass


@attr.s(auto_attribs=True, frozen=True)
class Question:
    name: str  # lowercased, no trailing dot
    qtype: int


@attr.s(auto_attribs=True, frozen=True)
class Query:
    id: int
    questions: tuple[Question, ...]


def _read_name(buf: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) name at ``offset``; returns the name and the offset just past it."""
    labels: list[str] = []
    end: int | None = None
    for _ in range(128):  # bounds pointer loops
        if offset >= len(buf):
            raise MalformedPacket("name runs past end of packet")
        length = buf[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(buf):
                raise MalformedPacket("truncated compression pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | buf[offset + 1]
            continue
        if length == 0:
            return ".".join(labels).lower(), end if end is not None else offset + 1
        label = buf[offset + 1 : offset + 1 + length]
        if len(label) != length:
            raise MalformedPacket("truncated label")
        labels.append(label.decode("utf-8", errors="replace"))
        offset += 1 + length
    raise MalformedPacket("compression pointer loop")


def parse_query(buf: bytes) -> Query | None:
    """The questions in ``buf``, or None if it isn't a standard query (eg it's someone's response)."""
    if len(buf) < _HEADER.size:
        raise MalformedPacket("short header")
    query_id, flags, qdcount, _, _, _ = _HEADER.unpack_from(buf)
    if flags & (_FLAG_RESPONSE | _OPCODE_MASK):
        return None
    offset = _HEADER.size
    questions: list[Question] = []
    for _ in range(qdcount):
        name, offset = _read_name(buf, offset)
        if offset + _TYPE_CLASS.size > len(buf):
            raise MalformedPacket("truncated question")
        qtype, _qclass = _TYPE_CLASS.unpack_from(buf, offset)
        offset += _TYPE_CLASS.size
        questions.append(Question(name=name, qtype=qtype))
    return Query(id=query_id, questions=tuple(questions))


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label for label in (p.encode() for p in name.split("."))) + b"\0"


def _record(name: str, rtype: int, ttl: int, cache_flush: bool, rdata: bytes) -> bytes:
    rclass = _CLASS_IN | (_CACHE_FLUSH if cache_flush else 0)
    return _encode_name(name) + _RR_FIXED.pack(rtype, rclass, ttl, len(rdata)) + rdata


def a_record(name: str, ip: str, ttl: int, cache_flush: bool) -> bytes:
    return _record(name, TYPE_A, ttl, cache_flush, socket.inet_aton(ip))


def nsec_a_only(name: str, ttl: int, cache_flush: bool) -> bytes:
    """An NSEC asserting ``name`` has an A record and nothing else, so a querier asking for AAAA gets
    an immediate negative answer rather than waiting out a timeout (RFC 6762 section 6.1)."""
    # window block 0, one bitmap byte, bit 1 (type A) set
    bitmap = bytes([0, 1, 0x40])
    return _record(name, TYPE_NSEC, ttl, cache_flush, _encode_name(name) + bitmap)


def build_response(
    query_id: int,
    questions: tuple[Question, ...],
    answers: list[bytes],
    additionals: list[bytes],
) -> bytes:
    """A response packet.  ``questions`` is echoed only for legacy unicast replies; multicast replies
    carry none (RFC 6762 section 6)."""
    header = _HEADER.pack(
        query_id, _FLAG_RESPONSE | _FLAG_AUTHORITATIVE, len(questions), len(answers), 0, len(additionals)
    )
    question_bytes = b"".join(_encode_name(q.name) + _TYPE_CLASS.pack(q.qtype, _CLASS_IN) for q in questions)
    return header + question_bytes + b"".join(answers) + b"".join(additionals)
