"""Validating records on the way in.

Provider-side, like the grant check: the zone file is generated from stored records, so a value
that doesn't parse would make CoreDNS reject the whole zone and take the domain down.  Catching it
here turns that into a 400 for one caller.
"""

from __future__ import annotations

import re

import dns.rdata
import dns.rdataclass
import dns.rdatatype
from dns.exception import DNSException

from compute_space.core.dns.service_api import APEX
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import RecordType
from compute_space.core.dns.service_api import normalize_zone

_LABEL_RE = re.compile(r"^[a-z0-9_*-]+$")


class InvalidRecord(ValueError):
    """A record that can't be written as given."""

    error_code = "invalid_record"


def normalize(record: DnsRecord, zone: str = "", *, allow_rrset_selector: bool = False) -> DnsRecord:
    """Canonicalize and validate a record, returning the form to store."""
    name = _normalize_name(record.name, zone)
    try:
        rrtype = RecordType(record.type.strip().upper())
    except ValueError:
        supported = ", ".join(sorted(RecordType))
        raise InvalidRecord(f"record type {record.type!r} is not writable (supported: {supported})") from None

    data = (record.data or "").strip() or None
    if data is None:
        if not allow_rrset_selector:
            # Omitting data means "whatever is there now", which only makes sense when removing.
            raise InvalidRecord(f"record {name} {rrtype} has no data")
        return DnsRecord(name=name, type=rrtype, ttl=record.ttl)

    try:
        rdata = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rrtype), data)
    except DNSException as e:
        raise InvalidRecord(f"invalid {rrtype} data {data!r}: {e}") from e
    # from_text derives A vs AAAA from the address family, so an "A" holding an IPv6 literal comes
    # back as AAAA.  Without this an app granted A could write an AAAA.
    if dns.rdatatype.to_text(rdata.rdtype) != rrtype:
        raise InvalidRecord(f"{data!r} is not valid {rrtype} data")
    # Store the canonical form, so the rendered zone file is always parseable.
    return DnsRecord(name=name, type=rrtype, ttl=record.ttl, data=rdata.to_text())


def _normalize_name(name: str, zone: str) -> str:
    """Fully-qualified input is rejected rather than fixed up: a zone file reads
    ``www.example.com`` inside zone ``example.com`` as ``www.example.com.example.com``."""
    n = name.strip().lower()
    if not n:
        raise InvalidRecord(f"record name is empty (use {APEX!r} for the zone apex)")
    if n == APEX:
        return APEX
    if n.endswith("."):
        raise InvalidRecord(f"record name {name!r} is fully qualified; names are relative to the zone")
    z = normalize_zone(zone)
    if z and (n == z or n.endswith("." + z)):
        raise InvalidRecord(f"record name {name!r} already includes the zone {z!r}; names are relative")
    for label in n.split("."):
        if not label or len(label) > 63 or not _LABEL_RE.match(label):
            raise InvalidRecord(f"record name {name!r} has an invalid label {label!r}")
    return n
