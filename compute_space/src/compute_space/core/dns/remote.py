"""The DnsBackend that calls whichever app provides the ``dns`` service.

The router is not an app, so it has no app token and no grants of its own.  It does have
something better: it is the sole authority for the ``X-OpenHost-*`` identity headers in the first
place (``_sanitize_forwarded_headers`` strips every inbound one, and app ports are published on
host loopback only).  So instead of proxying through ``/api/services/v2/call``, this dials the
provider app's loopback port directly and injects the same headers the proxy would have — with a
consumer id reserved for the router and a grant set computed from the domains it actually manages.

That grant set is deliberately narrow.  Blanket ``**`` access would work, but the provider app's
audit log is one of the few places an owner can see what touched their registrar, and "the router
may write ``_acme-challenge.*`` and the apex/wildcard A records for these three domains" is a far
more useful line than "the router may do anything".
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import attr
import httpx

from compute_space.core.dns.backend import DnsBackendError
from compute_space.core.dns.backend import UnknownZone
from compute_space.core.dns.records import APEX
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import normalize_record
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger

# Reserved consumer identity for the router's own service calls.  Not an installable app name —
# app names are DNS-label-like (see core.app_id), so the leading underscore cannot collide.
ROUTER_CONSUMER_ID = "_openhost_router"
ROUTER_CONSUMER_NAME = "OpenHost Router"

# External providers are slower than a local file by orders of magnitude, and some registrars
# take minutes to publish.  The ACME retry loop is still the backstop.
_REMOTE_PROPAGATION_TIMEOUT_SECONDS = 600.0

_REQUEST_TIMEOUT_SECONDS = 60.0


def router_grants(domains: list[str]) -> list[dict[str, Any]]:
    """The grants the router asserts for itself: challenge TXT plus the records it maintains.

    One pair of patterns per domain rather than a global wildcard, so the provider's own grant
    check still bounds what a bug here could touch.  Names are zone-relative and the zone is not
    known until the provider resolves it, so each domain contributes both the "this domain is the
    zone apex" form and the "this domain is a subdomain of the zone" form.
    """
    grants: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, rrtype: str) -> None:
        if (name, rrtype) not in seen:
            seen.add((name, rrtype))
            grants.append({"grant": {"name": name, "type": rrtype, "access": "rw"}, "scope": "global"})

    for domain in domains:
        labels = domain.strip(".").lower()
        # When the provider's zone *is* this domain, our records are at "@", "*" and
        # "_acme-challenge"; when the zone is a parent, they are prefixed with the leading labels.
        for prefix in (APEX, labels):
            base = "" if prefix == APEX else prefix
            add(_join(base, "_acme-challenge"), "TXT")
            add(base or APEX, "A")
            add(_join(base, "*"), "A")
    return grants


def _join(base: str, leaf: str) -> str:
    return f"{leaf}.{base}" if base else leaf


@attr.s(auto_attribs=True, frozen=True)
class ServiceDnsBackend:
    """Talks the ``dns`` service API to a provider app over loopback."""

    base_url: str
    permissions_header: str
    client: httpx.Client

    @classmethod
    def create(cls, port: int, endpoint: str, domains: list[str]) -> ServiceDnsBackend:
        return cls(
            base_url=f"http://127.0.0.1:{port}/{endpoint.strip('/')}",
            permissions_header=json.dumps(router_grants(domains)),
            client=httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS),
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> ServiceDnsBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def propagation_timeout_seconds(self) -> float:
        return _REMOTE_PROPAGATION_TIMEOUT_SECONDS

    def zones(self) -> list[str]:
        body = self._call("/zones", {})
        zones = body.get("zones")
        if not isinstance(zones, list):
            raise DnsBackendError(f"DNS service returned no zone list: {body}")
        return [str(z) for z in zones]

    def get_records(self, zone: str, name: str | None = None, rrtype: str | None = None) -> list[DnsRecord]:
        payload: dict[str, Any] = {"zone": zone}
        if name is not None:
            payload["name"] = name
        if rrtype is not None:
            payload["type"] = rrtype
        return self._records_from(self._call("/records/get", payload), zone)

    def set_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        return self._write("/records/set", zone, records)

    def append_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        return self._write("/records/append", zone, records)

    def delete_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        return self._write("/records/delete", zone, records, allow_rrset_selector=True)

    # ─── internals ───

    def _write(
        self, path: str, zone: str, records: list[DnsRecord], *, allow_rrset_selector: bool = False
    ) -> list[DnsRecord]:
        normalized = [normalize_record(r, zone, allow_rrset_selector=allow_rrset_selector) for r in records]
        payload = {"zone": zone, "records": [_to_wire(r) for r in normalized]}
        return self._records_from(self._call(path, payload), zone)

    def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(
                self.base_url + path,
                json=payload,
                headers={
                    "X-OpenHost-Consumer-Id": ROUTER_CONSUMER_ID,
                    "X-OpenHost-Consumer-Name": ROUTER_CONSUMER_NAME,
                    "X-OpenHost-Permissions": self.permissions_header,
                },
            )
        except httpx.HTTPError as e:
            raise DnsBackendError(f"DNS service unreachable at {self.base_url}: {e}") from e

        try:
            body = response.json()
        except ValueError as e:
            raise DnsBackendError(f"DNS service returned non-JSON ({response.status_code})") from e
        if not isinstance(body, dict):
            raise DnsBackendError(f"DNS service returned unexpected body: {body!r}")

        # 207 is a partial success across zones; the per-zone errors surface in _records_from.
        if response.status_code not in (200, 207):
            error = body.get("error", "unknown_error")
            message = body.get("message", "")
            if error == "unknown_zone":
                raise UnknownZone(f"DNS service does not manage this zone: {message}")
            raise DnsBackendError(f"DNS service call to {path} failed ({response.status_code}): {error} {message}")
        return body

    def _records_from(self, body: dict[str, Any], zone: str) -> list[DnsRecord]:
        """Flatten the service's per-zone results, failing loudly if our zone was not applied.

        The service fans out and reports per zone; we always name exactly one, so anything other
        than that zone succeeding is an error for us even when the overall status was 207.
        """
        results = body.get("results")
        if not isinstance(results, list):
            raise DnsBackendError(f"DNS service returned no results: {body!r}")
        out: list[DnsRecord] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            if not result.get("ok"):
                raise DnsBackendError(f"DNS service failed for zone {result.get('zone', zone)}: {result.get('error')}")
            for rec in result.get("records") or []:
                out.append(
                    DnsRecord(
                        name=str(rec.get("name", "")),
                        type=str(rec.get("type", "")),
                        ttl=int(rec.get("ttl", 0)),
                        data=rec.get("data"),
                    )
                )
        if not results:
            logger.warning(f"DNS service reported no zones for {zone}")
        return out


def _to_wire(record: DnsRecord) -> dict[str, Any]:
    """The service's record shape.  Data is omitted entirely for an RRset selector, which is how
    the API spells "delete whatever is at this name and type"."""
    wire: dict[str, Any] = {"name": record.name, "type": record.type, "ttl": record.ttl}
    if record.data is not None:
        wire["data"] = record.data
    return wire


def domains_for_grants(db: sqlite3.Connection) -> list[str]:
    """The non-mDNS domains the router manages, used to scope its self-asserted grants."""
    return [d.name_no_port for d in effective_domains(db) if not d.mdns]
