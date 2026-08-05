"""Owner-authed API to manage the domains an instance answers on at runtime.

Adding a TLS domain kicks off ACME acquisition in the background (the same
``ensure_cert_for`` routine used at initial setup); the domain is served immediately via
Caddy's internal CA and flips to its real cert when acquisition completes.  Adding an mDNS
`.local` domain is active immediately (served over http).  All changes update the active
config (so routing sees them) and regenerate + restart Caddy (so it terminates/serves them).
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing

import anyio
import attr
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.background_tasks import BackgroundTask
from litestar.di import NamedDependency
from litestar.enums import MediaType
from litestar.params import FromPath

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.caddy import reload_caddy_for_domains
from compute_space.core.dns import reload_coredns_for_domains
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import effective_domains
from compute_space.core.domains import get_record
from compute_space.core.domains import load_records
from compute_space.core.domains import primary_domain
from compute_space.core.domains import remove_record
from compute_space.core.domains import set_record_status
from compute_space.core.domains import upsert_record
from compute_space.core.logging import logger
from compute_space.core.tls.domain_certs import ensure_cert_for
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status
from compute_space.db import get_db
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.routes.api.responses import ErrorResponse
from compute_space.web.routes.api.responses import error_spec
from compute_space.web.routes.api.responses import response_spec

# A DNS label per RFC 1123 (letters/digits/hyphen, not starting/ending with hyphen), and a
# name is one-or-more labels joined by dots (so it has at least one dot: `foo.local`, not `foo`).
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})+$")


@attr.s(auto_attribs=True, frozen=True)
class AddDomainRequest:
    name: str
    tls: bool = False
    mdns: bool = False


@attr.s(auto_attribs=True, frozen=True)
class DomainInfo:
    name: str
    tls: bool
    mdns: bool
    scheme: str
    cert_status: DomainCertStatus
    error_message: str | None
    is_primary: bool


@attr.s(auto_attribs=True, frozen=True)
class DomainListResponse:
    domains: list[DomainInfo]


def _tls_cert_display(
    config: Config, name: str, record: DomainRecord | None, is_primary: bool
) -> tuple[DomainCertStatus, str | None]:
    """Cert status for a TLS domain, derived from the cert actually on disk (what Caddy serves) so an
    expired/unreadable cert is never shown 'active'; falls back to the stored in-flight state."""
    status = get_cert_status(config.cert_path_for(name, is_primary), config.key_path_for(name, is_primary))
    if status in (CertStatus.OK, CertStatus.EXPIRING_SOON):
        return DomainCertStatus.ACTIVE, None  # a valid cert is on disk
    if status == CertStatus.EXPIRED:
        # On disk but no longer valid: browsers reject it and renewal is failing — surface it.
        return DomainCertStatus.ERROR, (
            record.error_message if record else None
        ) or "certificate expired or unreadable"
    if record is not None:
        return record.cert_status, record.error_message  # MISSING: acquiring / error / none
    return DomainCertStatus.NONE, None


def _domain_info(config: Config, domain: Domain, record: DomainRecord | None) -> DomainInfo:
    name = domain.name_no_port
    is_primary = record.is_primary if record is not None else False
    if not domain.tls:
        cert_status, error = DomainCertStatus.ACTIVE, None  # http, nothing to acquire
    else:
        cert_status, error = _tls_cert_display(config, name, record, is_primary)
    return DomainInfo(
        name=name,
        tls=domain.tls,
        mdns=domain.mdns,
        scheme=domain.scheme,
        cert_status=cert_status,
        error_message=error,
        is_primary=is_primary,
    )


def _domain_list(config: Config, db: sqlite3.Connection) -> list[DomainInfo]:
    """The API view of the full domain set, loading all records in one query."""
    return [_domain_info(config, r.to_domain(), r) for r in load_records(db)]


def _run_acquisition(config: Config, domain: Domain) -> None:
    """Acquire the domain's cert, then flip its status + reload Caddy so it uses the real cert.
    Runs off the request thread (acquisition is slow), so it owns one DB connection for the job.
    Records the error on failure."""
    with closing(get_db()) as db:
        try:
            ensure_cert_for(config, domain, db)
        except Exception as exc:  # noqa: BLE001 — surface any acquisition failure as domain status
            logger.opt(exception=True).error("cert acquisition failed for {}", domain.name)
            set_record_status(db, domain.name_no_port, DomainCertStatus.ERROR, error_message=str(exc))
            return
        set_record_status(db, domain.name_no_port, DomainCertStatus.ACTIVE)
        # Regenerate Caddy from the *live* active config, not the snapshot captured at add time — a
        # domain added while this (slow) acquisition ran would otherwise be dropped from the Caddyfile.
        reload_caddy_for_domains(get_config(), db)


def _spawn_acquisition(config: Config, domain: Domain) -> None:
    """Start background cert acquisition.  Indirected through this function so tests can run it
    synchronously."""
    threading.Thread(target=_run_acquisition, args=(config, domain), daemon=True).start()


async def _reload_caddy_after_response() -> None:
    """Regenerate + gracefully reload Caddy as a response background task.

    The reload is zero-downtime (admin-API config reload — see CaddyProcess.reload), so it won't drop
    the request that triggered it; running it after the response is sent just keeps the reload's
    latency off the response path.  Reads the *live* config so a concurrent domain change isn't
    dropped from the regenerated Caddyfile; off the event loop since the reload shells out."""

    def _reload() -> None:
        with closing(get_db()) as db:
            reload_caddy_for_domains(get_config(), db)

    await anyio.to_thread.run_sync(_reload)


def _validate_new_domain(config: Config, name: str, tls: bool, mdns: bool, db: sqlite3.Connection) -> str | None:
    if not name:
        return "domain name is required"
    if not _DOMAIN_RE.match(name):
        return "invalid domain name"
    if mdns and tls:
        return "mDNS (.local) domains are served over http; set tls=false"
    if any(d.name_no_port == name for d in effective_domains(db)):
        return "domain is already configured"
    return None


@get("/api/domains", guards=[require_owner_auth])
async def list_domains(config: NamedDependency[Config], db: NamedDependency[sqlite3.Connection]) -> DomainListResponse:
    return DomainListResponse(domains=_domain_list(config, db))


@post(
    "/api/domains",
    status_code=202,
    guards=[require_owner_auth],
    responses={
        202: response_spec(DomainListResponse, "Domain accepted; any cert acquisition runs in the background"),
        400: error_spec("Invalid, duplicate, or TLS-flagged mDNS name"),
    },
)
async def add_domain(
    data: AddDomainRequest,
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
) -> Response[DomainListResponse] | Response[ErrorResponse]:
    name = data.name.strip().lower()
    error = _validate_new_domain(config, name, data.tls, data.mdns, db)
    if error is not None:
        return Response(ErrorResponse(error=error), status_code=400, media_type=MediaType.JSON)

    domain = Domain(name=name, tls=data.tls, mdns=data.mdns)
    # TLS domains start as `acquiring` (served via `tls internal` until the real cert lands);
    # non-TLS (.local) domains are immediately active over http.
    upsert_record(
        db,
        DomainRecord(
            name=name,
            tls=data.tls,
            mdns=data.mdns,
            cert_status=DomainCertStatus.ACQUIRING if data.tls else DomainCertStatus.ACTIVE,
        ),
    )
    if not data.mdns:
        # Make CoreDNS authoritative for the new public zone *before* acquisition: DNS-01 writes the
        # _acme-challenge TXT into this domain's zone file, which only resolves once CoreDNS serves
        # the zone.  Run off the event loop — the restart does a blocking terminate+wait(3s) — but
        # await it so ordering before acquisition holds.  (mDNS domains never touch CoreDNS.)
        await anyio.to_thread.run_sync(reload_coredns_for_domains, config, db)
    if data.tls:
        _spawn_acquisition(config, domain)
    # Return the full updated list so the client repaints the table without a follow-up GET, and
    # regenerate Caddy (serving the new site) only after this response has been sent — see
    # _reload_caddy_after_response.
    return Response(
        DomainListResponse(domains=_domain_list(config, db)),
        status_code=202,
        media_type=MediaType.JSON,
        background=BackgroundTask(_reload_caddy_after_response),
    )


@delete(
    "/api/domains/{name:str}",
    status_code=200,
    guards=[require_owner_auth],
    responses={
        200: response_spec(DomainListResponse, "The remaining domains"),
        400: error_spec("The primary domain cannot be removed"),
        404: error_spec("No such domain"),
    },
)
async def remove_domain(
    name: FromPath[str],
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
) -> Response[DomainListResponse] | Response[ErrorResponse]:
    name = name.strip().lower()
    if name == primary_domain(db).name_no_port:
        return Response(ErrorResponse(error="cannot remove the primary domain"), status_code=400)
    removed = get_record(db, name)
    if not remove_record(db, name):
        return Response(ErrorResponse(error="domain not found"), status_code=404)
    if removed is not None and not removed.mdns:
        # Drop the zone from CoreDNS so it stops answering for the removed public domain.  Off the
        # event loop — the restart blocks on a terminate+wait(3s).
        await anyio.to_thread.run_sync(reload_coredns_for_domains, config, db)
    # Regenerate Caddy only after this response has been sent — see _reload_caddy_after_response.
    return Response(
        DomainListResponse(domains=_domain_list(config, db)),
        status_code=200,
        media_type=MediaType.JSON,
        background=BackgroundTask(_reload_caddy_after_response),
    )


api_domains_routes = Router(path="/", route_handlers=[list_domains, add_domain, remove_domain])
