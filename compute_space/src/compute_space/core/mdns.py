"""Custom Wildcard mDNS responder for the instance's ``.local`` domains.
*.[hostname(s)].local -> ip"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from collections.abc import Sequence

import attr

from compute_space.core.domains import Domain
from compute_space.core.logging import logger

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

# Precompiled layouts for every fixed-width field group on the wire — struct.Struct compiles the
# format once, so a hot parse/build path doesn't re-parse "!HHH" et al. on every call, and
# unpack_from reads straight out of the receive buffer instead of slicing a copy first.
_HEADER = struct.Struct("!HHHHHH")  # id, flags, qdcount, ancount, nscount, arcount
_TYPE_CLASS = struct.Struct("!HH")  # a question's qtype + qclass, or a query's qtype + qclass
_RR_FIXED = struct.Struct("!HHIH")  # an RR's type, class, ttl, rdlength (name and rdata sit outside)


def _is_local_source(
    ip: str,
    our_ip6: str | None = None,
    our_ip6_prefix: int = 64,
    our_ip: str | None = None,
    our_ip_prefix: int | None = None,
) -> bool:
    """True if ``ip`` is a peer on our local link — the only ones we answer.

    The sockets bind the wildcard address, so they also receive *unicast* datagrams sent straight to
    a routable address; answering those would make an internet-facing box an mDNS reflection vector
    (and leak the LAN IP).

    IPv4 LAN senders are private, but a private address alone isn't enough: two boxes can each sit on
    an unrelated RFC 1918 range that isn't actually the same broadcast domain (a routed corporate net,
    a VPN client, a Docker bridge, ...).  When our actual subnet is known (``our_ip``/``our_ip_prefix``,
    read from the connected route — see ``_interface_ipv4_prefix_len``) senders are narrowed to it;
    without it we fall back to accepting any private v4 sender, same as before that could be measured.

    IPv6 LAN senders often hold a *global* SLAAC address, so those are accepted only when they share
    our on-link prefix (``our_ip6_prefix`` — the box's actual configured prefix length, not assumed to
    be /64: a home or corporate LAN is /64, but a cloud VM's interface is often a bare /128, which
    correctly excludes every peer since there's no broadcast domain to trust there).  A v6 link-local
    or ULA sender is unambiguously on-link regardless of prefix — unlike v4 private ranges, those
    families are non-routable beyond the link by definition.
    """
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.is_private:  # link-local (fe80::/10) or ULA (fc00::/7)
            return True
        if our_ip6 is None:
            return False
        return addr in ipaddress.IPv6Network(f"{our_ip6}/{our_ip6_prefix}", strict=False)
    if not addr.is_private:  # v4 RFC 1918 (and other reserved ranges) is the LAN-address heuristic
        return False
    if our_ip is None or our_ip_prefix is None:
        return True  # real subnet not discovered; same broad "any private sender" behavior as before
    return addr in ipaddress.IPv4Network(f"{our_ip}/{our_ip_prefix}", strict=False)


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
    ident, flags, qdcount, _ancount, _nscount, _arcount = _HEADER.unpack_from(data)
    is_query = (flags >> 15) & 1 == 0
    offset = 12
    questions: list[_Question] = []
    for _ in range(qdcount):
        name, offset = _decode_name(data, offset)
        qtype, qclass = _TYPE_CLASS.unpack_from(data, offset)
        offset += _TYPE_CLASS.size
        questions.append(_Question(name=name, qtype=qtype, qclass=qclass))
    return ident, is_query, tuple(questions)


def _parse_a_answers(data: bytes) -> tuple[tuple[str, str], ...] | None:
    """The (owner name, IP) of every A record in a *response*'s answer section, or None if it isn't a
    response.  Used only by the startup conflict probe, so it decodes just what it needs."""
    if len(data) < 12:
        return None
    _ident, flags, qdcount, ancount, _nscount, _arcount = _HEADER.unpack_from(data)
    if (flags >> 15) & 1 == 0:
        return None
    offset = 12
    for _ in range(qdcount):
        _name, offset = _decode_name(data, offset)
        offset += _TYPE_CLASS.size
    records: list[tuple[str, str]] = []
    for _ in range(ancount):
        name, offset = _decode_name(data, offset)
        rtype, _rclass, _ttl, rdlen = _RR_FIXED.unpack_from(data, offset)
        offset += _RR_FIXED.size
        if rtype == _TYPE_A and rdlen == 4:
            records.append((name, socket.inet_ntoa(data[offset : offset + 4])))
        offset += rdlen
    return tuple(records)


def _a_record(name: str, ip: str, ttl: int, cache_flush: bool) -> bytes:
    rrclass = _CLASS_IN | (_CACHE_FLUSH if cache_flush else 0)
    rdata = socket.inet_aton(ip)
    return _encode_name(name) + _RR_FIXED.pack(_TYPE_A, rrclass, ttl, len(rdata)) + rdata


def _aaaa_record(name: str, ip6: str, ttl: int, cache_flush: bool) -> bytes:
    rrclass = _CLASS_IN | (_CACHE_FLUSH if cache_flush else 0)
    rdata = socket.inet_pton(socket.AF_INET6, ip6)
    return _encode_name(name) + _RR_FIXED.pack(_TYPE_AAAA, rrclass, ttl, len(rdata)) + rdata


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
    return _encode_name(name) + _RR_FIXED.pack(_TYPE_NSEC, rrclass, ttl, len(rdata)) + rdata


def _dedupe(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


def _build_query(name: str) -> bytes:
    return _HEADER.pack(0, 0, 1, 0, 0, 0) + _encode_name(name) + _TYPE_CLASS.pack(_TYPE_A, _CLASS_IN)


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
    header = _HEADER.pack(ident if legacy else 0, 0x8400, len(echoed), len(answers), 0, len(additional))
    body = b"".join(_encode_name(q.name) + _TYPE_CLASS.pack(q.qtype, q.qclass & ~_QU_BIT) for q in echoed)
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
    lan_ip_prefix: int | None
    lan_ip6: str | None
    lan_ip6_prefix: int
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
        if not _is_local_source(str(addr[0]), ip6, self.lan_ip6_prefix, our_ip=ip, our_ip_prefix=self.lan_ip_prefix):
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
    try:
        _share_port(sock)
        sock.bind(("", _MDNS_PORT))
        _join_group(sock, lan_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)  # RFC 6762 §11: mDNS uses TTL 255
    except OSError:
        sock.close()
        raise
    sock.settimeout(1.0)  # so _serve_loop wakes to observe _stop
    return sock


def _connected_ipv4_routes() -> list[ipaddress.IPv4Network]:
    """Every directly-connected (gatewayless) IPv4 route's network, read from ``/proc/net/route``.

    Kernel format: whitespace-separated ``Iface Destination Gateway Flags ... Mask ...``, with
    ``Destination``/``Gateway``/``Mask`` as 32-bit hex in the machine's native byte order (reversed
    relative to dotted-decimal on the little-endian deploy target).  A route with an all-zero gateway
    is on-link — the kernel's own record of the subnet actually configured on an interface, unlike a
    forwarded route (a real gateway) or the default route.
    """
    networks: list[ipaddress.IPv4Network] = []
    try:
        with open("/proc/net/route") as f:
            lines = f.read().splitlines()
    except OSError:
        return networks
    for line in lines[1:]:  # skip the header row
        parts = line.split()
        if len(parts) < 8 or parts[2] != "00000000":  # has a real gateway -> not on-link
            continue
        try:
            dest = socket.inet_ntoa(struct.pack("<L", int(parts[1], 16)))
            mask = socket.inet_ntoa(struct.pack("<L", int(parts[7], 16)))
            networks.append(ipaddress.IPv4Network(f"{dest}/{mask}", strict=False))
        except (ValueError, struct.error):
            continue
    return networks


def _interface_ipv4_prefix_len(ip: str) -> int | None:
    """The prefix length of the connected route that contains ``ip``, or None when it can't be
    determined (off Linux, or no matching connected route — e.g. a cloud VM with only host routes).

    There's no /64-like universal default for IPv4 subnet sizes (home LANs are commonly /24, corporate
    nets vary widely), so unlike the IPv6 prefix lookup this has no guessed fallback — callers treat
    None as "unknown" and degrade to the pre-existing broad behavior rather than assume a specific size.
    """
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    for network in _connected_ipv4_routes():
        if addr in network:
            return network.prefixlen
    return None


def _if_inet6_fields(ip6: str) -> list[str] | None:
    """The ``/proc/net/if_inet6`` line for ``ip6``, split into fields, or None off Linux / not found.

    Kernel format: ``<address, 32 hex chars><devno hex><prefix length hex><scope hex><flags hex><name>``.
    """
    packed = socket.inet_pton(socket.AF_INET6, ip6).hex()
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[0] == packed:
                    return parts
    except (OSError, ValueError):
        pass
    return None


def _interface_index(ip6: str) -> int:
    """Kernel index of the interface holding ``ip6``; 0 lets the kernel choose.

    IPv6 multicast joins take an interface index, not an address.  ``/proc/net/if_inet6`` is the
    stdlib-free way to map one to the other on Linux (the deploy target); elsewhere we fall back to
    the default interface rather than not joining at all.
    """
    fields = _if_inet6_fields(ip6)
    return int(fields[1], 16) if fields else 0


def _interface_prefix_len(ip6: str) -> int:
    """The interface's actually-configured prefix length for ``ip6``, or 64 when it can't be read.

    Home/corporate LANs are /64 (SLAAC requires it — RFC 7381 recommends the same for enterprise
    subnets), so 64 is a safe fallback; but some cloud VMs configure a bare /128 on the interface
    itself (routing the rest of the subnet off-link), where treating it as /64 would wrongly accept
    other tenants' addresses as on-link peers.  Reading the real value avoids assuming either way.
    """
    fields = _if_inet6_fields(ip6)
    return int(fields[2], 16) if fields else 64


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


def _probe_conflict(sock: socket.socket, name: str, our_ip: str, our_ip_prefix: int | None = None) -> str | None:
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
                data, addr = sock.recvfrom(9000)
            except TimeoutError:
                continue
            except OSError:
                return None
            if not _is_local_source(str(addr[0]), our_ip=our_ip, our_ip_prefix=our_ip_prefix):
                continue  # off-link/spoofed sender — same boundary _handle enforces for real queries
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


def mdns_bases(domains: Sequence[Domain]) -> tuple[str, ...]:
    """The port-stripped names of every mDNS (``.local``) domain the instance answers on."""
    return tuple(d.name_no_port for d in domains if d.is_local)


def start_mdns(bases: tuple[str, ...], lan_ip: str, lan_ip6: str | None = None) -> MdnsResponder:
    """Bind the mDNS sockets, warn on any pre-existing claimant, and start answering for ``bases``."""
    transports = _open_transports(lan_ip, lan_ip6)
    lan_ip_prefix = _interface_ipv4_prefix_len(lan_ip)
    for base in bases:  # probe on IPv4 only — a name clash is a clash whatever family finds it
        conflict = _probe_conflict(transports[0].sock, base, lan_ip, lan_ip_prefix)
        if conflict is not None:
            logger.warning("mDNS: {} already claimed on the LAN by {}; serving {} anyway", base, conflict, lan_ip)
    lan_ip6_prefix = _interface_prefix_len(lan_ip6) if lan_ip6 is not None else 64
    responder = MdnsResponder(
        transports=transports,
        lan_ip=lan_ip,
        lan_ip_prefix=lan_ip_prefix,
        lan_ip6=lan_ip6,
        lan_ip6_prefix=lan_ip6_prefix,
        bases=bases,
    )
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


def ensure_mdns_for_domains(domains: Sequence[Domain], lan_ip: str | None = None, lan_ip6: str | None = None) -> None:
    """Reconcile the responder with ``domains``' ``.local`` (mDNS) entries: start it when the first
    one appears, refresh its served set, or stop it when the last one is removed.  In-process, no
    restart.  Called at boot and by /api/domains, so mDNS turns on/off at runtime."""
    bases = mdns_bases(domains)
    responder = get_active_mdns()
    if not bases:
        if responder is not None:
            responder.stop()
            set_active_mdns(None)
            logger.info("Stopped mDNS responder (no .local domains)")
        return
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
