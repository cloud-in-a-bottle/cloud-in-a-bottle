"""Custom Wildcard mDNS responder for the instance's ``.local`` domains.
*.[hostname(s)].local -> ip"""

from __future__ import annotations

import ipaddress
import socket
import sqlite3
import struct
import threading
import time

import attr

from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.util import default_route_source_ip

_MDNS_GROUP = "224.0.0.251"
_MDNS_GROUP6 = "ff02::fb"
_MDNS_PORT = 5353
_TYPE_A = 1
_TYPE_AAAA = 28
_TYPE_NSEC = 47
_TYPE_ANY = 255
_CLASS_IN = 0x0001
_CACHE_FLUSH = 0x8000  # top bit of an rrclass: responder tells peers to flush prior records
_QU_BIT = 0x8000  # top bit of a question's qclass: querier wants a unicast response
_TTL = 120  # seconds; short so a moved instance is re-learned quickly
_LEGACY_TTL = 10  # RFC 6762 §6.7: legacy unicast responses cap TTL at 10s
_PROBE_BUDGET = 0.75  # seconds; overall deadline so ambient LAN chatter can't keep the probe alive


def _is_local_source(ip: str, our_ip6: str | None = None) -> bool:
    """True if ``ip`` is a peer on our local link — the only ones we answer.

    The sockets bind the wildcard address, so they also receive *unicast* datagrams sent straight to
    a routable address; answering those would make an internet-facing box an mDNS reflection vector
    (and leak the LAN IP).  IPv4 LAN senders are always private.  IPv6 LAN senders often hold a
    *global* SLAAC address, so those are accepted only when they share our ``/64``.
    """
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if addr.is_private:  # v4 RFC 1918, or v6 link-local (fe80::/10) / ULA (fc00::/7)
        return True
    if not isinstance(addr, ipaddress.IPv6Address) or our_ip6 is None:
        return False
    return addr in ipaddress.IPv6Network(f"{our_ip6}/64", strict=False)


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


def _parse_questions(data: bytes) -> tuple[int, bool, tuple[_Question, ...]]:
    """Parse a message's header + question section.  Returns ``(ident, is_query, questions)``; answer/
    authority sections are ignored (we only respond to queries)."""
    if len(data) < 12:
        raise ValueError("short DNS packet")
    ident, flags, qdcount = struct.unpack("!HHH", data[:6])
    is_query = (flags >> 15) & 1 == 0
    offset = 12
    questions: list[_Question] = []
    for _ in range(qdcount):
        name, offset = _decode_name(data, offset)
        qtype, qclass = struct.unpack("!HH", data[offset : offset + 4])
        offset += 4
        questions.append(_Question(name=name, qtype=qtype, qclass=qclass))
    return ident, is_query, tuple(questions)


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


def _aaaa_record(name: str, ip6: str, ttl: int, cache_flush: bool) -> bytes:
    rrclass = _CLASS_IN | (_CACHE_FLUSH if cache_flush else 0)
    rdata = socket.inet_pton(socket.AF_INET6, ip6)
    return _encode_name(name) + struct.pack("!HHIH", _TYPE_AAAA, rrclass, ttl, len(rdata)) + rdata


def _type_bitmap(types: tuple[int, ...]) -> bytes:
    """RFC 4034 §4.1.2 type bit map.  Every type we serve is < 256, so it's a single window-0 block."""
    width = max(types) // 8 + 1
    bitmap = bytearray(width)
    for rrtype in types:
        bitmap[rrtype // 8] |= 0x80 >> (rrtype % 8)
    return bytes([0, width]) + bytes(bitmap)


def _nsec_record(name: str, served: tuple[int, ...], ttl: int, cache_flush: bool) -> bytes:
    """RFC 6762 §6.1 negative response: ``name`` exists with exactly ``served`` and nothing else.

    ``getaddrinfo`` asks A and AAAA together and waits for both, so without this a client's query for
    a type we don't have stalls until its resolver times out (5s on macOS/glibc).  The bitmap must
    list every type we actually serve — it is a positive denial of everything omitted, which clients
    cache for the TTL.
    """
    rrclass = _CLASS_IN | (_CACHE_FLUSH if cache_flush else 0)
    rdata = _encode_name(name) + _type_bitmap(served)  # mDNS: next-domain-name is the record's own name
    return _encode_name(name) + struct.pack("!HHIH", _TYPE_NSEC, rrclass, ttl, len(rdata)) + rdata


def _dedupe(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


def _build_query(name: str) -> bytes:
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_name(name) + struct.pack("!HH", _TYPE_A, _CLASS_IN)


def _build_response(questions: tuple[_Question, ...], ip: str, ip6: str | None, legacy: bool, ident: int) -> bytes:
    """A response to every question in ``questions`` (all of which we own): the address records for
    the types asked for, and an NSEC denial for a type we don't serve.

    The other served type and an NSEC always ride along in the additional section, so a client that
    asked for A learns the AAAA (or its absence) from the same packet and its parallel query needn't
    wait.  Multicast responses omit the question, zero the transaction ID and set the cache-flush bit
    (RFC 6762 §6); legacy unicast responses (querier not on :5353) echo the query's ID and question,
    use short TTLs, and never set cache-flush — a stub resolver drops an answer whose ID differs.
    """
    ttl = _LEGACY_TTL if legacy else _TTL
    flush = not legacy
    served = (_TYPE_A, _TYPE_AAAA) if ip6 is not None else (_TYPE_A,)

    def _record(rrtype: int, name: str) -> bytes:
        if rrtype == _TYPE_A:
            return _a_record(name, ip, ttl, flush)
        assert ip6 is not None  # AAAA is in `served` only when we have an address
        return _aaaa_record(name, ip6, ttl, flush)

    answers: list[bytes] = []
    additional: list[bytes] = []
    for name in _dedupe(tuple(q.name for q in questions)):
        asked = {q.qtype for q in questions if q.name == name}
        wanted = tuple(t for t in served if t in asked or _TYPE_ANY in asked)
        if not wanted:
            answers.append(_nsec_record(name, served, ttl, flush))
            continue
        answers += [_record(t, name) for t in wanted]
        additional += [_record(t, name) for t in served if t not in wanted]
        additional.append(_nsec_record(name, served, ttl, flush))

    echoed = questions if legacy else ()
    header = struct.pack("!HHHHHH", ident if legacy else 0, 0x8400, len(echoed), len(answers), 0, len(additional))
    body = b"".join(_encode_name(q.name) + struct.pack("!HH", q.qtype, q.qclass & ~_QU_BIT) for q in echoed)
    return header + body + b"".join(answers) + b"".join(additional)


# --- responder -------------------------------------------------------------------------------


@attr.s(auto_attribs=True, frozen=True)
class Transport:
    """One mDNS socket and the multicast address group responses go to on its address family."""

    sock: socket.socket
    group: tuple[object, ...]


@attr.s(auto_attribs=True)
class MdnsResponder:
    """Answers ``.local`` queries for ``_bases`` (and their ``*.base`` subdomains) with ``lan_ip``
    and, when the box has one, ``lan_ip6``.  One thread per transport (IPv4 multicast, and IPv6 when
    available); ``update()`` swaps the served set live, ``stop()`` tears it all down."""

    transports: tuple[Transport, ...]
    lan_ip: str
    lan_ip6: str | None
    _bases: tuple[str, ...]
    _stop: threading.Event = attr.ib(factory=threading.Event, init=False, eq=False, repr=False)
    _lock: threading.Lock = attr.ib(factory=threading.Lock, init=False, eq=False, repr=False)
    _threads: list[threading.Thread] = attr.ib(factory=list, init=False, eq=False, repr=False)

    def start(self) -> None:
        for i, transport in enumerate(self.transports):
            thread = threading.Thread(target=self._serve_loop, args=(transport,), name=f"mdns-{i}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def update(self, bases: tuple[str, ...]) -> None:
        """Swap the served set.  The addresses are fixed for the sockets' lifetime — they're baked
        into the group memberships, so a moved address is a rebind (see ``ensure_mdns_for_domains``)."""
        with self._lock:
            self._bases = bases

    def _owns(self, qname: str) -> bool:
        q = _normalize(qname)
        with self._lock:
            return any(q == b or q.endswith("." + b) for b in self._bases)

    def _serve_loop(self, transport: Transport) -> None:
        while not self._stop.is_set():
            try:
                data, addr = transport.sock.recvfrom(9000)
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break  # stop() closed the socket to unblock us
                logger.opt(exception=True).warning("mDNS: recvfrom failed; retrying")
                self._stop.wait(1.0)  # throttle so a persistent error can't spin
                continue
            try:
                self._handle(transport, data, addr)
            except Exception:  # noqa: BLE001 — a malformed peer packet must not kill the responder
                logger.opt(exception=True).warning("mDNS: dropping unhandled query from {}", addr)

    def _handle(self, transport: Transport, data: bytes, addr: tuple[object, ...]) -> None:
        with self._lock:
            ip, ip6 = self.lan_ip, self.lan_ip6
        if not _is_local_source(str(addr[0]), ip6):
            return  # never answer a routed unicast query from off-LAN
        ident, is_query, questions = _parse_questions(data)
        if not is_query:
            return
        # Every question for a name we own gets a reply — an address record, or an NSEC denial for a
        # type we don't serve.  Staying silent leaves the querier waiting out its resolver timeout.
        matched = tuple(q for q in questions if self._owns(q.name))
        if not matched:
            return
        legacy = addr[1] != _MDNS_PORT  # querier on an ephemeral port -> conventional unicast DNS
        packet = _build_response(matched, ip, ip6, legacy, ident)
        wants_unicast = legacy or any(q.qclass & _QU_BIT for q in matched)
        transport.sock.sendto(packet, addr if wants_unicast else transport.group)

    def stop(self) -> None:
        self._stop.set()
        for transport in self.transports:
            try:
                transport.sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)


def _join_group(sock: socket.socket, lan_ip: str) -> None:
    """Join 224.0.0.251 on the interface that owns ``lan_ip``.

    A wildcard join lets the kernel pick one interface off the route to the group — the default-route
    NIC — so on a multi-homed box (separate WAN/LAN NICs, or a VPN owning the default route) queries
    arriving on the LAN would never reach us.  Falls back to the wildcard if that interface is gone.
    """
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan_ip))
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(_MDNS_GROUP) + socket.inet_aton(lan_ip)
        )
    except OSError:
        logger.warning("mDNS: cannot join 224.0.0.251 on {}; using the default interface", lan_ip)
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            struct.pack("=4sl", socket.inet_aton(_MDNS_GROUP), socket.INADDR_ANY),
        )


def _share_port(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        # Share :5353 with the OS mDNS stack (macOS mDNSResponder / Linux avahi) instead of fighting it.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)


def _open_socket(lan_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _share_port(sock)
    sock.bind(("", _MDNS_PORT))
    _join_group(sock, lan_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)  # RFC 6762 §11: mDNS uses TTL 255
    sock.settimeout(1.0)  # so _serve_loop wakes to observe _stop
    return sock


def _interface_index(ip6: str) -> int:
    """Kernel index of the interface holding ``ip6``; 0 lets the kernel choose.

    IPv6 multicast joins take an interface index, not an address.  ``/proc/net/if_inet6`` is the
    stdlib-free way to map one to the other on Linux (the deploy target); elsewhere we fall back to
    the default interface rather than not joining at all.
    """
    packed = socket.inet_pton(socket.AF_INET6, ip6).hex()
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == packed:
                    return int(parts[1], 16)
    except (OSError, ValueError):
        pass
    return 0


def _open_socket6(lan_ip6: str) -> socket.socket:
    """An IPv6 mDNS socket joined to ff02::fb on the interface holding ``lan_ip6``."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        # v6-only: the IPv4 transport is its own socket, and a dual-stack bind would collide with it.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        _share_port(sock)
        sock.bind(("", _MDNS_PORT))
        index = _interface_index(lan_ip6)
        mreq = socket.inet_pton(socket.AF_INET6, _MDNS_GROUP6) + struct.pack("@I", index)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        if index:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, struct.pack("@I", index))
    except (OSError, AttributeError):
        sock.close()
        raise
    sock.settimeout(1.0)
    return sock


def _open_transports(lan_ip: str, lan_ip6: str | None) -> tuple[Transport, ...]:
    """The IPv4 transport, plus the IPv6 one when the box has an address for it.

    IPv6 is best-effort: a kernel without it, or a join the interface won't accept, must not cost us
    the IPv4 responder — we still serve AAAA *records* over IPv4, only v6-only queriers lose out.
    """
    transports = [Transport(sock=_open_socket(lan_ip), group=(_MDNS_GROUP, _MDNS_PORT))]
    if lan_ip6 is not None:
        try:
            index = _interface_index(lan_ip6)
            transports.append(
                Transport(sock=_open_socket6(lan_ip6), group=(_MDNS_GROUP6, _MDNS_PORT, 0, index)),
            )
        except (OSError, AttributeError) as exc:
            logger.warning(f"mDNS: no IPv6 transport ({exc}); serving AAAA over IPv4 only")
    return tuple(transports)


def _probe_conflict(sock: socket.socket, name: str, our_ip: str) -> str | None:
    """Send one query for ``name`` and briefly listen; returns another host's IP if one already
    answers for it, else None.  Best-effort — the caller warns and serves anyway (no renaming)."""
    want = _normalize(name)
    # A busy LAN (Bonjour/AirPlay/Chromecast) delivers unrelated packets faster than the per-recv
    # timeout, so only an overall deadline bounds this — and it blocks boot and /api/domains.
    deadline = time.monotonic() + _PROBE_BUDGET
    sock.settimeout(0.25)
    try:
        sock.sendto(_build_query(name), (_MDNS_GROUP, _MDNS_PORT))
        while time.monotonic() < deadline:
            try:
                data, _addr = sock.recvfrom(9000)
            except (TimeoutError, OSError):
                return None
            try:
                records = _parse_a_answers(data)
            except (ValueError, IndexError, struct.error, OSError):
                continue  # malformed LAN chatter (OSError: inet_ntoa on truncated rdata) — keep listening
            for rname, ip in records or ():
                if _normalize(rname) == want and ip != our_ip:
                    return ip
        return None
    finally:
        sock.settimeout(1.0)


def mdns_bases(db: sqlite3.Connection) -> tuple[str, ...]:
    """The port-stripped names of every mDNS (``.local``) domain the instance answers on."""
    return tuple(d.name_no_port for d in effective_domains(db) if d.is_local)


def start_mdns(bases: tuple[str, ...], lan_ip: str, lan_ip6: str | None = None) -> MdnsResponder:
    """Bind the mDNS sockets, warn on any pre-existing claimant, and start answering for ``bases``."""
    transports = _open_transports(lan_ip, lan_ip6)
    for base in bases:  # probe on IPv4 only — a name clash is a clash whatever family finds it
        conflict = _probe_conflict(transports[0].sock, base, lan_ip)
        if conflict is not None:
            logger.warning("mDNS: {} already claimed on the LAN by {}; serving {} anyway", base, conflict, lan_ip)
    responder = MdnsResponder(transports=transports, lan_ip=lan_ip, lan_ip6=lan_ip6, bases=bases)
    responder.start()
    served = lan_ip if lan_ip6 is None else f"{lan_ip}, {lan_ip6}"
    logger.info("Started mDNS responder for {} -> {}", ", ".join(bases), served)
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


def _start_and_register(bases: tuple[str, ...], lan_ip: str, lan_ip6: str | None) -> None:
    try:
        set_active_mdns(start_mdns(bases, lan_ip, lan_ip6))
    except OSError as exc:
        # A :5353 bind clash (e.g. an avahi/systemd-resolved without SO_REUSEPORT) must never take
        # the router down — degrade to no mDNS and keep serving.
        logger.warning("mDNS responder failed to start ({}); continuing without it", exc)


def ensure_mdns_for_domains(db: sqlite3.Connection, lan_ip: str | None = None, lan_ip6: str | None = None) -> None:
    """Reconcile the responder with the DB's ``.local`` (mDNS) domains: start it when the first one
    appears, refresh its served set, or stop it when the last one is removed.  In-process, no
    restart.  Called at boot and by /api/domains, so mDNS turns on/off at runtime."""
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
        _start_and_register(bases, lan_ip, lan_ip6)
    elif lan_ip is not None and (lan_ip, lan_ip6) != (responder.lan_ip, responder.lan_ip6):
        # (A vanished LAN IP falls through to `update`: keep serving the last known address rather
        # than tearing down the responder over a transient lookup failure.)
        # Group memberships are pinned to the interfaces the sockets were opened on, so a moved
        # address needs fresh sockets — not just a new advertised IP.
        logger.info(
            "mDNS: addresses moved {} -> {}; rebinding responder",
            (responder.lan_ip, responder.lan_ip6),
            (lan_ip, lan_ip6),
        )
        responder.stop()
        set_active_mdns(None)
        _start_and_register(bases, lan_ip, lan_ip6)
    else:
        responder.update(bases)
