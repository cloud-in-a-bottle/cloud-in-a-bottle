from __future__ import annotations

import asyncio
import errno
import socket
import struct

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


class MdnsResponder:
    """Answers mDNS queries for the instance's ``.local`` domains and every name under them, so
    ``myapp.myhost.local`` resolves on the LAN without any per-app registration.

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

    @property
    def domains(self) -> tuple[str, ...]:
        return self._domains

    async def update(self, domains: tuple[Domain, ...]) -> None:
        """Publish exactly the mDNS domains among ``domains``, starting or stopping the listener as needed."""
        self._domains = tuple(d.name_no_port for d in domains if d.mdns)
        if self._domains and self._sock is None:
            self._start()
        elif not self._domains and self._sock is not None:
            await self.stop()
        if self._domains:
            logger.info(f"mDNS: publishing {', '.join(self._domains)} and every name under them")

    def _start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)  # RFC 6762 section 11
        sock.bind(("", self._port))
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
                pass
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
            pktinfo = next(
                (d for level, kind, d in ancdata if level == socket.IPPROTO_IP and kind == socket.IP_PKTINFO), None
            )
            if pktinfo is None:
                raise RuntimeError("mDNS: received a packet without IP_PKTINFO, which the socket enables")
            self._handle(data, pktinfo, addr)

    def _handle(self, data: bytes, pktinfo: bytes, addr: tuple[str, int]) -> None:
        assert self._sock is not None
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
        hostname = socket.gethostname().split(".")[0].lower()
        reply = build_answer(query, addr[1], self._domains, hostname, local_ip)
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
