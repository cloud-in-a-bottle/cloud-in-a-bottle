"""DNS for the instance: the CoreDNS process, the record backends, and the ``dns`` service.

Where things live:

* ``coredns`` — the CoreDNS child process, the Corefile, and zone-file seeding.  Serves two
  independent things: the public authoritative zones (only when this instance is its own DNS
  provider) and the container view (always, since the app hairpin needs it either way).
* ``records`` — the record shape every backend speaks, plus what may be written.
* ``zonefile`` — read/modify/write a CoreDNS zone file, which is the source of truth for the
  records it holds.
* ``backend`` — the DnsBackend interface and the DNS-01 helpers built on it.
* ``local`` / ``remote`` — the two implementations: our own zone files, or the app providing the
  ``dns`` service.
* ``selection`` — which of those two this space actually uses.
* ``service`` — the router's own implementation of the ``dns`` service, for apps that want to
  write records.
* ``public_ip`` / ``dynamic`` — where the instance thinks it is, and keeping that up to date.
"""

from compute_space.core.dns.backend import DnsBackend
from compute_space.core.dns.backend import DnsBackendError
from compute_space.core.dns.backend import UnknownZone
from compute_space.core.dns.backend import clear_txt
from compute_space.core.dns.backend import publish_txt
from compute_space.core.dns.backend import wait_for_txt_propagation
from compute_space.core.dns.coredns import CoreDnsProcess
from compute_space.core.dns.coredns import DnsZone
from compute_space.core.dns.coredns import coredns_is_needed
from compute_space.core.dns.coredns import get_active_coredns
from compute_space.core.dns.coredns import public_dns_zones
from compute_space.core.dns.coredns import reload_coredns_for_domains
from compute_space.core.dns.coredns import set_active_coredns
from compute_space.core.dns.coredns import start_coredns
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord
from compute_space.core.dns.records import ReservedRecord
from compute_space.core.dns.selection import dns_backend
from compute_space.core.dns.selection import uses_local_dns

__all__ = [
    "CoreDnsProcess",
    "DnsBackend",
    "DnsBackendError",
    "DnsRecord",
    "DnsZone",
    "InvalidRecord",
    "ReservedRecord",
    "UnknownZone",
    "clear_txt",
    "coredns_is_needed",
    "dns_backend",
    "get_active_coredns",
    "public_dns_zones",
    "publish_txt",
    "reload_coredns_for_domains",
    "set_active_coredns",
    "start_coredns",
    "uses_local_dns",
    "wait_for_txt_propagation",
]
