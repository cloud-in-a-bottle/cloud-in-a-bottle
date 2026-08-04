"""Custom Wildcard mDNS responder for the instance's ``.local`` domains.
*.[hostname(s)].local -> ip"""

from __future__ import annotations

import ipaddress
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


def _is_local_source(ip: str) -> bool:
    """True if ``ip`` is on a private/link-local/loopback network — the only peers we answer.

    The socket binds ``0.0.0.0:5353`` so it also receives *unicast* datagrams sent straight to a
    public IP; answering those would make an internet-facing box an mDNS reflection/amplification
    vector (and leak the LAN IP).  LAN multicast senders are always private, so this is transparent.
    """
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


# --- wire format -----------------------------------------------------------------------------


def _normalize(name: str) -> str:
    return name.rstrip(".").lower()


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
    lowest_pointer = len(data)  # RFC 1035: each pointer must jump strictly backwards, so this bounds the loop
    total = 0
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if not jumped:
                resume = offset + 2
            target = ((length & 0x3F) << 8) | data[offset + 1]
            if target >= lowest_pointer:
                raise ValueError("cyclic or forward mDNS compression pointer")
            lowest_pointer = target
            offset = target
            jumped = True
            continue
        offset += 1
        if length == 0:
            break
        total += length + 1
        if total > 255:
            raise ValueError("mDNS name too long")
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


def _parse_a_answers(data: bytes) -> tuple[tuple[str, str], ...] | None:
    """The (owner name, IP) of every A record in a *response*'s answer section, or None if it isn't a
    response.  Used only by the startup conflict probe, so it decodes just what it needs."""
    if len(data) < 12:
        return None
    _ident, flags, qdcount, ancount = struct.unpack("!HHHH", data[:8])
    if (flags >> 15) & 1 == 0:
        return None
    offset = 12
    for _ in range(qdcount):
        _name, offset = _decode_name(data, offset)
        offset += 4
    records: list[tuple[str, str]] = []
    for _ in range(ancount):
        name, offset = _decode_name(data, offset)
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if rtype == _TYPE_A and rdlen == 4:
            records.append((name, socket.inet_ntoa(data[offset : offset + 4])))
        offset += rdlen
    return tuple(records)


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
        q = _normalize(qname)
        with self._lock:
            return any(q == b or q.endswith("." + b) for b in self._bases)

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(9000)
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break  # stop() closed the socket to unblock us
                logger.opt(exception=True).warning("mDNS: recvfrom failed; retrying")
                self._stop.wait(1.0)  # throttle so a persistent error can't spin
                continue
            try:
                self._handle(data, addr)
            except Exception:  # noqa: BLE001 — a malformed peer packet must not kill the responder
                logger.opt(exception=True).warning("mDNS: dropping unhandled query from {}", addr)

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        if not _is_local_source(addr[0]):
            return  # never answer a routed unicast query from off-LAN
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
    want = _normalize(name)
    sock.settimeout(0.25)
    try:
        sock.sendto(_build_query(name), (_MDNS_GROUP, _MDNS_PORT))
        while True:
            try:
                data, _addr = sock.recvfrom(9000)
            except (TimeoutError, OSError):
                return None
            try:
                records = _parse_a_answers(data)
            except (ValueError, IndexError, struct.error):
                continue  # malformed LAN chatter — keep listening
            for rname, ip in records or ():
                if _normalize(rname) == want and ip != our_ip:
                    return ip
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


def ensure_mdns_for_domains(db: sqlite3.Connection, lan_ip: str | None = None) -> None:
    """Reconcile the responder with the DB's ``.local`` (mDNS) domains: start it when the first one
    appears, refresh its served set + LAN IP, or stop it when the last one is removed.  In-process,
    no restart.  Called at boot and by /api/domains, so mDNS turns on/off at runtime."""
    bases = mdns_bases(db)
    responder = get_active_mdns()
    if not bases:
        if responder is not None:
            responder.stop()
            set_active_mdns(None)
            logger.info("Stopped mDNS responder (no .local domains)")
        return
    if lan_ip is None:
        lan_ip = default_route_source_ip()
    if responder is None:
        if lan_ip is None:
            logger.warning("mDNS domain configured but no LAN IP found; responder not started")
            return
        try:
            set_active_mdns(start_mdns(bases, lan_ip))
        except OSError as exc:
            # A :5353 bind clash (e.g. an avahi/systemd-resolved without SO_REUSEPORT) must never take
            # the router down — degrade to no mDNS and keep serving.
            logger.warning("mDNS responder failed to start ({}); continuing without it", exc)
    else:
        responder.update(bases, lan_ip or responder.lan_ip)
