"""Resolve a name over multicast DNS the way a LAN client does, and print the address.

Used by smoke_test.sh inside the booted image (stdlib only, so it runs on a stock Ubuntu). Sends a
query to the mDNS group from port 5353 (so the reply is multicast too, as for a real querier) and
waits for a response that answers the name.

Usage: python3 mdns_query.py <name>
"""

import socket
import struct
import sys
import time

GROUP = "224.0.0.251"
PORT = 5353


def encode(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def main() -> None:
    name = sys.argv[1].lower()
    qname = encode(name)
    query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(1)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        sock.sendto(query, (GROUP, PORT))
        try:
            while True:
                data = sock.recv(9000)
                _, flags, qd, an, _, _ = struct.unpack_from("!HHHHHH", data)
                # Our responder's multicast replies carry no question section and lead with the A
                # record for the asked name, uncompressed.
                if not flags & 0x8000 or qd or an < 1 or not data[12:].startswith(qname):
                    continue
                off = 12 + len(qname)
                rtype, _, _, rdlen = struct.unpack_from("!HHIH", data, off)
                if rtype == 1 and rdlen == 4:
                    print(f"{name} -> {socket.inet_ntoa(data[off + 10 : off + 14])}")
                    return
        except TimeoutError:
            continue
    sys.exit(f"no mDNS answer for {name}")


main()
