from __future__ import annotations

import asyncio
import errno
import socket
import struct
import time

from compute_space.core.domains import Domain
from compute_space.core.logging import logger
from compute_space.core.mdns.answers import MDNS_PORT
from compute_space.core.mdns.answers import build_answer
from compute_space.core.mdns.packets import MalformedPacket
from compute_space.core.mdns.packets import parse_query

_MDNS_GROUP = "224.0.0.251"
# struct in_pktinfo: ifindex, local (interface) address, header destination address
_PKTINFO = struct.Struct("@i4s4s")
# struct ip_mreqn: group, interface address (any), ifindex
_MREQN = struct.Struct("@4s4si")
# How often to look for new interfaces to join the group on (a USB NIC plugged in after boot, etc).
_REJOIN_SECONDS = 30
_MAX_PACKET = 9000
# A packet that trips a bug is logged at most this often, so a querier repeating it can't flood the log.
_ERROR_LOG_INTERVAL_SECONDS = 60


class MdnsResponder:
    """Answers mDNS queries for every name under the instance's ``.local`` domains, so
    ``myapp.myhost.local`` resolves on the LAN without any per-app registration.  The bare domain is
    the machine's own ``<hostname>.local``, which avahi publishes; this only adds the subdomains.

    Each query is answered with the address of the interface it arrived on, so the answer tracks DHCP
    renewals and interface changes without any polling.  The socket shares port 5353 with avahi (or any
    other responder) via SO_REUSEADDR; each of them gets a copy of every multicast query.

    Only listens while at least one ``.local`` domain is configured (see ``update``)."""

    def __init__(self, port: int = MDNS_PORT) -> None:
        self._port = port
        self._domains: tuple[str, ...] = ()
        self._sock: socket.socket | None = None
        self._joined: set[int] = set()
        self._rejoin_task: asyncio.Task[None] | None = None
        self._last_error_logged = float("-inf")

    @property
    def domains(self) -> tuple[str, ...]:
        return self._domains

    async def update(self, domains: tuple[Domain, ...]) -> None:
        """Publish exactly the mDNS domains among ``domains``, starting or stopping the listener as needed.

        mDNS is an extra, so failing to listen is logged rather than raised: the router still serves
        everything else, and the next call (a domain change, or the next boot) tries again."""
        self._domains = tuple(d.name_no_port for d in domains if d.mdns)
        if self._domains and self._sock is None:
            try:
                self._start()
            except OSError:
                logger.exception(
                    f"mDNS: can't listen on udp {self._port}, so names under {', '.join(self._domains)} won't "
                    "resolve on the LAN (another process may hold the port without SO_REUSEADDR); "
                    "retrying on the next domain change or restart"
                )
                return
        elif not self._domains and self._sock is not None:
            await self.stop()
        if self._domains:
            logger.info(f"mDNS: publishing every name under {', '.join(self._domains)}")

    def _start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)  # RFC 6762 section 11
        # Bound to the group, not to every address: the kernel then delivers only datagrams sent to
        # the mDNS group (from the link, since multicast to 224.0.0.0/24 isn't routed), never unicast
        # from elsewhere.  Replies still get a real source address, from the interface they leave on.
        try:
            sock.bind((_MDNS_GROUP, self._port))
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        self._sock = sock
        asyncio.get_running_loop().add_reader(sock.fileno(), self._on_readable)
        self._join_new_interfaces()
        self._rejoin_task = asyncio.create_task(self._keep_joined())

    async def stop(self) -> None:
        if self._rejoin_task is not None:
            self._rejoin_task.cancel()
            try:
                await self._rejoin_task
            except asyncio.CancelledError:
                pass  # the cancellation we just asked for
            self._rejoin_task = None
        if self._sock is not None:
            asyncio.get_running_loop().remove_reader(self._sock.fileno())
            self._sock.close()
            self._sock = None
        self._joined.clear()

    async def _keep_joined(self) -> None:
        while True:
            await asyncio.sleep(_REJOIN_SECONDS)
            self._join_new_interfaces()

    def _join_new_interfaces(self) -> None:
        assert self._sock is not None
        current = dict(socket.if_nameindex())
        # A removed interface takes its membership with it; forget it so it is rejoined if it returns.
        self._joined &= current.keys()
        for index, name in current.items():
            if index in self._joined:
                continue
            mreq = _MREQN.pack(socket.inet_aton(_MDNS_GROUP), socket.inet_aton("0.0.0.0"), index)
            try:
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as e:
                if e.errno != errno.EADDRINUSE:  # EADDRINUSE: already a member
                    # Interfaces without IPv4 (eg some tunnels) refuse; nothing to answer there anyway.
                    logger.debug(f"mDNS: not joining on {name}: {e}")
                    continue
            self._joined.add(index)

    def _on_readable(self) -> None:
        assert self._sock is not None
        while True:
            try:
                data, ancdata, _flags, addr = self._sock.recvmsg(_MAX_PACKET, socket.CMSG_SPACE(_PKTINFO.size))
            except BlockingIOError:
                return
            # One bad packet mustn't stop the rest: anything it raises is a bug, logged (rate-limited,
            # since anyone on the LAN can resend it) and skipped.
            try:
                self._handle(data, ancdata, addr)
            except Exception:
                now = time.monotonic()
                if now - self._last_error_logged >= _ERROR_LOG_INTERVAL_SECONDS:
                    self._last_error_logged = now
                    logger.exception(f"mDNS: failed to handle a packet from {addr}; skipping it")

    def _handle(self, data: bytes, ancdata: list[tuple[int, int, bytes]], addr: tuple[str, int]) -> None:
        assert self._sock is not None
        pktinfo = next(
            (d for level, kind, d in ancdata if level == socket.IPPROTO_IP and kind == socket.IP_PKTINFO), None
        )
        if pktinfo is None:
            raise RuntimeError("received a packet without IP_PKTINFO, which the socket enables")
        ifindex, local_addr, _ = _PKTINFO.unpack_from(pktinfo)
        local_ip = socket.inet_ntoa(local_addr)
        if local_ip == "0.0.0.0":
            return  # arrived on an interface with no IPv4 address; nothing to tell the querier
        try:
            query = parse_query(data)
        except MalformedPacket:
            return
        if query is None:
            return
        reply = build_answer(query, addr[1], self._domains, local_ip)
        if reply is None:
            return
        # Legacy (non-5353) queriers get a unicast reply; everyone else a multicast one, so other
        # hosts' caches are refreshed too.  Either way it leaves through the interface it came in on.
        dest = addr if addr[1] != MDNS_PORT else (_MDNS_GROUP, MDNS_PORT)
        out_pktinfo = _PKTINFO.pack(ifindex, local_addr, bytes(4))
        try:
            self._sock.sendmsg([reply], [(socket.IPPROTO_IP, socket.IP_PKTINFO, out_pktinfo)], 0, dest)
        except OSError as e:
            # eg the interface went away between receive and send; the querier will retry.
            logger.debug(f"mDNS: reply to {addr} failed: {e}")
