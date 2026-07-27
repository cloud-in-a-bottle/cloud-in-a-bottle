"""Owner-authed API to manage the domains an instance answers on at runtime.

Adding a TLS domain kicks off ACME acquisition in the background (the same
``ensure_cert_for`` routine used at initial setup); the domain is served immediately via
Caddy's internal CA and flips to its real cert when acquisition completes.  Adding an mDNS
`.local` domain is active immediately (served over http).  All changes update the active
config (so routing sees them) and regenerate + restart Caddy (so it terminates/serves them).
"""

from __future__ import annotations

import re
import threading
import time

import attr
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.enums import MediaType

from compute_space.config import Config
from compute_space.config import Domain
from compute_space.config import get_config
from compute_space.core.caddy import reload_caddy_for_domains
from compute_space.core.dns import reload_coredns_for_domains
from compute_space.core.domain_store import CERT_STATUS_ACQUIRING
from compute_space.core.domain_store import CERT_STATUS_ACTIVE
from compute_space.core.domain_store import CERT_STATUS_ERROR
from compute_space.core.domain_store import CERT_STATUS_NONE
from compute_space.core.domain_store import DomainRecord
from compute_space.core.domain_store import get_record
from compute_space.core.domain_store import rebuild_active_domains
from compute_space.core.domain_store import remove_record
from compute_space.core.domain_store import set_record_status
from compute_space.core.domain_store import upsert_record
from compute_space.core.logging import logger
from compute_space.core.tls.domain_certs import ensure_cert_for
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status
from compute_space.web.auth.auth import require_owner_auth

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
    cert_status: str
    error_message: str | None
    is_primary: bool


@attr.s(auto_attribs=True, frozen=True)
class DomainListResponse:
    domains: list[DomainInfo]


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str


def _tls_cert_display(config: Config, name: str, record: DomainRecord | None) -> tuple[str, str | None]:
    """Cert status for a TLS domain, derived from the cert actually on disk (what Caddy serves) so an
    expired/unreadable cert is never shown 'active'; falls back to the stored in-flight state."""
    status = get_cert_status(config.cert_path_for(name), config.key_path_for(name))
    if status in (CertStatus.OK, CertStatus.EXPIRING_SOON):
        return CERT_STATUS_ACTIVE, None  # a valid cert is on disk
    if status == CertStatus.EXPIRED:
        # On disk but no longer valid: browsers reject it and renewal is failing — surface it.
        return CERT_STATUS_ERROR, (record.error_message if record else None) or "certificate expired or unreadable"
    if record is not None:
        return record.cert_status, record.error_message  # MISSING: acquiring / error / none
    return CERT_STATUS_NONE, None


def _domain_info(config: Config, domain: Domain) -> DomainInfo:
    name = domain.name_no_port
    is_primary = name == config.primary_domain.name_no_port
    if not domain.tls:
        cert_status, error = CERT_STATUS_ACTIVE, None  # http, nothing to acquire
    else:
        cert_status, error = _tls_cert_display(config, name, get_record(config, name))
    return DomainInfo(
        name=name,
        tls=domain.tls,
        mdns=domain.mdns,
        scheme=domain.scheme,
        cert_status=cert_status,
        error_message=error,
        is_primary=is_primary,
    )


def _run_acquisition(config: Config, domain: Domain) -> None:
    """Acquire the domain's cert, then flip its status + reload Caddy so it uses the real cert.
    Runs off the request thread (acquisition is slow).  Records the error on failure."""
    try:
        ensure_cert_for(config, domain)
    except Exception as exc:  # noqa: BLE001 — surface any acquisition failure as domain status
        logger.opt(exception=True).error("cert acquisition failed for {}", domain.name)
        set_record_status(config, domain.name_no_port, CERT_STATUS_ERROR, error_message=str(exc))
        return
    set_record_status(config, domain.name_no_port, CERT_STATUS_ACTIVE)
    # Regenerate Caddy from the *live* active config, not the snapshot captured at add time — a
    # domain added while this (slow) acquisition ran would otherwise be dropped from the Caddyfile.
    reload_caddy_for_domains(get_config())


def _spawn_acquisition(config: Config, domain: Domain) -> None:
    """Start background cert acquisition.  Indirected through this function so tests can run it
    synchronously."""
    threading.Thread(target=_run_acquisition, args=(config, domain), daemon=True).start()


def _reload_caddy_after_response() -> None:
    """Restart Caddy off the request path (a daemon thread, after a short flush delay).

    The dashboard is served *through* Caddy (browser → Caddy:443 → router), so restarting Caddy
    *inside* the request that triggered a domain change would tear down that request's own connection
    before the response reaches the browser — leaving the UI stale until a manual refresh.  Deferring
    the restart lets the response (the full domain list) land first.  CoreDNS restarts and cert
    acquisition stay inline: they're on port 53 / ACME, not the dashboard's HTTP path."""

    def work() -> None:
        time.sleep(0.5)  # let the response finish flushing through Caddy before we restart it
        reload_caddy_for_domains(get_config())

    threading.Thread(target=work, daemon=True).start()


def _validate_new_domain(config: Config, name: str, tls: bool, mdns: bool) -> str | None:
    if not name:
        return "domain name is required"
    if not _DOMAIN_RE.match(name):
        return "invalid domain name"
    if mdns and tls:
        return "mDNS (.local) domains are served over http; set tls=false"
    if any(d.name_no_port == name for d in config.all_domains):
        return "domain is already configured"
    return None


@get("/api/domains", guards=[require_owner_auth])
async def list_domains(config: Config) -> DomainListResponse:
    return DomainListResponse(domains=[_domain_info(config, d) for d in config.all_domains])


@post("/api/domains", status_code=202, guards=[require_owner_auth])
async def add_domain(data: AddDomainRequest, config: Config) -> Response[DomainListResponse] | Response[ErrorResponse]:
    name = data.name.strip().lower()
    error = _validate_new_domain(config, name, data.tls, data.mdns)
    if error is not None:
        return Response(ErrorResponse(error=error), status_code=400, media_type=MediaType.JSON)

    domain = Domain(name=name, tls=data.tls, mdns=data.mdns)
    # TLS domains start as `acquiring` (served via `tls internal` until the real cert lands);
    # non-TLS (.local) domains are immediately active over http.
    upsert_record(
        config,
        DomainRecord(
            name=name,
            tls=data.tls,
            mdns=data.mdns,
            cert_status=CERT_STATUS_ACQUIRING if data.tls else CERT_STATUS_ACTIVE,
        ),
    )
    new_config = rebuild_active_domains(config)
    if not data.mdns:
        # Make CoreDNS authoritative for the new public zone *before* acquisition: DNS-01 writes
        # the _acme-challenge TXT into this domain's zone file, which only resolves once CoreDNS
        # serves the zone.  (mDNS domains never touch CoreDNS.)
        reload_coredns_for_domains(new_config)
    if data.tls:
        _spawn_acquisition(new_config, domain)
    _reload_caddy_after_response()  # serve the new site (tls internal for TLS) without killing this request
    # Return the full updated list so the client repaints the table without a follow-up GET that
    # would race the deferred Caddy restart.
    return Response(
        DomainListResponse(domains=[_domain_info(new_config, d) for d in new_config.all_domains]),
        status_code=202,
        media_type=MediaType.JSON,
    )


@delete("/api/domains/{name:str}", status_code=200, guards=[require_owner_auth])
async def remove_domain(name: str, config: Config) -> Response[DomainListResponse] | Response[ErrorResponse]:
    name = name.strip().lower()
    if name == config.primary_domain.name_no_port:
        return Response(ErrorResponse(error="cannot remove the primary domain"), status_code=400)
    removed = get_record(config, name)
    if not remove_record(config, name):
        return Response(ErrorResponse(error="domain not found"), status_code=404)
    new_config = rebuild_active_domains(config)
    if removed is not None and not removed.mdns:
        # Drop the zone from CoreDNS so it stops answering for the removed public domain.
        reload_coredns_for_domains(new_config)
    _reload_caddy_after_response()  # deferred so it doesn't tear down this request's connection
    return Response(
        DomainListResponse(domains=[_domain_info(new_config, d) for d in new_config.all_domains]),
        status_code=200,
        media_type=MediaType.JSON,
    )


api_domains_routes = Router(path="/", route_handlers=[list_domains, add_domain, remove_domain])
