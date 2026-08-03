"""Custom Wildcard mDNS responder for the instance's ``.local`` domains.
*.[hostname(s)].local -> ip"""

from __future__ import annotations

import socket
import sqlite3
import struct
import threading

import attr

from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.util import default_route_source_ip

_MDNS_GROUP = "224.0.0.251"
_MDNS_PORT = 5353
_TYPE_A = 1
_TYPE_ANY = 255
_CLASS_IN = 0x0001
_CACHE_FLUSH = 0x8000  # top bit of an rrclass: responder tells peers to flush prior records
_QU_BIT = 0x8000  # top bit of a question's qclass: querier wants a unicast response
_TTL = 120  # seconds; short so a moved instance is re-learned quickly
_LEGACY_TTL = 10  # RFC 6762 §6.7: legacy unicast responses cap TTL at 10s


# --- wire format -----------------------------------------------------------------------------


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        out.append(len(label))
        out += label.encode("ascii")
    out.append(0)
    return bytes(out)


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a DNS name, following compression pointers; returns the name and the offset just past
    it (past the pointer, when one was taken)."""
    labels: list[str] = []
    resume = offset
    jumped = False
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if not jumped:
                resume = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            jumped = True
            continue
        offset += 1
        if length == 0:
            break
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), (resume if jumped else offset)


@attr.s(auto_attribs=True, frozen=True)
class _Question:
    name: str
    qtype: int
    qclass: int


def _parse_questions(data: bytes) -> tuple[bool, tuple[_Question, ...]]:
    """Parse a message's header + question section.  Returns ``(is_query, questions)``; answer/
    authority sections are ignored (we only respond to queries)."""
    if len(data) < 12:
        raise ValueError("short DNS packet")
    _ident, flags, qdcount = struct.unpack("!HHH", data[:6])
    is_query = (flags >> 15) & 1 == 0
    offset = 12
    questions: list[_Question] = []
    for _ in range(qdcount):
        name, offset = _decode_name(data, offset)
        qtype, qclass = struct.unpack("!HH", data[offset : offset + 4])
        offset += 4
        questions.append(_Question(name=name, qtype=qtype, qclass=qclass))
    return is_query, tuple(questions)


def _parse_a_answers(data: bytes) -> tuple[str, ...] | None:
    """The IPs of every A record in a *response*'s answer section, or None if it isn't a response.
    Used only by the startup conflict probe, so it decodes just what it needs."""
    if len(data) < 12:
        return None
    _ident, flags, qdcount, ancount = struct.unpack("!HHHH", data[:8])
    if (flags >> 15) & 1 == 0:
        return None
    offset = 12
    for _ in range(qdcount):
        _name, offset = _decode_name(data, offset)
        offset += 4
    ips: list[str] = []
    for _ in range(ancount):
        _name, offset = _decode_name(data, offset)
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if rtype == _TYPE_A and rdlen == 4:
            ips.append(socket.inet_ntoa(data[offset : offset + 4]))
        offset += rdlen
    return tuple(ips)


def _a_record(name: str, ip: str, ttl: int, cache_flush: bool) -> bytes:
    rrclass = _CLASS_IN | (_CACHE_FLUSH if cache_flush else 0)
    rdata = socket.inet_aton(ip)
    return _encode_name(name) + struct.pack("!HHIH", _TYPE_A, rrclass, ttl, len(rdata)) + rdata


def _build_query(name: str) -> bytes:
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_name(name) + struct.pack("!HH", _TYPE_A, _CLASS_IN)


def _build_response(names: tuple[str, ...], ip: str, questions: tuple[_Question, ...], legacy: bool) -> bytes:
    """An mDNS/legacy-unicast response advertising ``ip`` for each name.

    Multicast responses omit the question and set the cache-flush bit (RFC 6762 §6); legacy unicast
    responses (querier not on :5353) echo the question, use short TTLs, and never set cache-flush.
    """
    ttl = _LEGACY_TTL if legacy else _TTL
    echoed = questions if legacy else ()
    header = struct.pack("!HHHHHH", 0, 0x8400, len(echoed), len(names), 0, 0)
    body = b"".join(_encode_name(q.name) + struct.pack("!HH", q.qtype, q.qclass & ~_QU_BIT) for q in echoed)
    body += b"".join(_a_record(name, ip, ttl, cache_flush=not legacy) for name in names)
    return header + body


# --- responder -------------------------------------------------------------------------------


@attr.s(auto_attribs=True)
class MdnsResponder:
    """Answers multicast ``.local`` queries for ``_bases`` (and their ``*.base`` subdomains) with
    ``lan_ip``.  In-process; ``update()`` swaps the served set live, ``stop()`` tears it down."""

    sock: socket.socket
    lan_ip: str
    _bases: tuple[str, ...]
    _stop: threading.Event = attr.ib(factory=threading.Event, init=False, eq=False, repr=False)
    _lock: threading.Lock = attr.ib(factory=threading.Lock, init=False, eq=False, repr=False)
    _thread: threading.Thread | None = attr.ib(default=None, init=False, eq=False, repr=False)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve_loop, name="mdns", daemon=True)
        self._thread.start()

    def update(self, bases: tuple[str, ...], lan_ip: str) -> None:
        with self._lock:
            self._bases = bases
            self.lan_ip = lan_ip

    def _owns(self, qname: str) -> bool:
        q = qname.rstrip(".").lower()
        with self._lock:
            return any(q == b or q.endswith("." + b) for b in self._bases)

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(9000)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                self._handle(data, addr)
            except Exception:  # noqa: BLE001 — a malformed peer packet must not kill the responder
                logger.opt(exception=True).warning("mDNS: dropping unhandled query from {}", addr)

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        is_query, questions = _parse_questions(data)
        if not is_query:
            return
        matched = tuple(q for q in questions if q.qtype in (_TYPE_A, _TYPE_ANY) and self._owns(q.name))
        if not matched:
            return
        legacy = addr[1] != _MDNS_PORT  # querier on an ephemeral port -> conventional unicast DNS
        with self._lock:
            ip = self.lan_ip
        packet = _build_response(tuple(q.name for q in matched), ip, matched, legacy)
        wants_unicast = legacy or any(q.qclass & _QU_BIT for q in matched)
        self.sock.sendto(packet, addr if wants_unicast else (_MDNS_GROUP, _MDNS_PORT))

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)


def _open_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        # Share :5353 with the OS mDNS stack (macOS mDNSResponder / Linux avahi) instead of fighting it.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", _MDNS_PORT))
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        struct.pack("=4sl", socket.inet_aton(_MDNS_GROUP), socket.INADDR_ANY),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)  # RFC 6762 §11: mDNS uses TTL 255
    sock.settimeout(1.0)  # so _serve_loop wakes to observe _stop
    return sock


def _probe_conflict(sock: socket.socket, name: str, our_ip: str) -> str | None:
    """Send one query for ``name`` and briefly listen; returns another host's IP if one already
    answers for it, else None.  Best-effort — the caller warns and serves anyway (no renaming)."""
    sock.settimeout(0.25)
    try:
        sock.sendto(_build_query(name), (_MDNS_GROUP, _MDNS_PORT))
        while True:
            try:
                data, _addr = sock.recvfrom(9000)
            except TimeoutError:
                return None
            except OSError:
                return None
            ips = _parse_a_answers(data)
            if ips and any(ip != our_ip for ip in ips):
                return next(ip for ip in ips if ip != our_ip)
    finally:
        sock.settimeout(1.0)


def mdns_bases(db: sqlite3.Connection) -> tuple[str, ...]:
    """The port-stripped names of every mDNS (``.local``) domain the instance answers on."""
    return tuple(d.name_no_port for d in effective_domains(db) if d.mdns)


def start_mdns(bases: tuple[str, ...], lan_ip: str) -> MdnsResponder:
    """Bind the mDNS socket, warn on any pre-existing claimant, and start answering for ``bases``."""
    sock = _open_socket()
    for base in bases:
        conflict = _probe_conflict(sock, base, lan_ip)
        if conflict is not None:
            logger.warning("mDNS: {} already claimed on the LAN by {}; serving {} anyway", base, conflict, lan_ip)
    responder = MdnsResponder(sock=sock, lan_ip=lan_ip, bases=bases)
    responder.start()
    logger.info("Started mDNS responder for {} -> {}", ", ".join(bases), lan_ip)
    return responder


# The live responder, registered by start.py so /api/domains can update the served set when a
# `.local` domain is added/removed.  Mirrors the active-CoreDNS/Caddy registries.  None when no
# mDNS domain is configured (the common public-domain case) or in dev/tests.
_active_mdns: MdnsResponder | None = None


def set_active_mdns(responder: MdnsResponder | None) -> None:
    global _active_mdns
    _active_mdns = responder


def get_active_mdns() -> MdnsResponder | None:
    return _active_mdns


def reload_mdns_for_domains(db: sqlite3.Connection) -> bool:
    """Update the running responder's served set (and re-read the LAN IP) from the DB's current mDNS
    domains.  In-process, no restart.  No-op (False) when the responder isn't running."""
    responder = get_active_mdns()
    if responder is None:
        return False
    responder.update(mdns_bases(db), default_route_source_ip() or responder.lan_ip)
    return True
