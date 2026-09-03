"""Owner-authed API to manage the domains an instance answers on at runtime.

Adding a TLS domain kicks off ACME acquisition in the background (the same
``ensure_cert_for`` routine used at initial setup); the domain is served immediately via
Caddy's internal CA and flips to its real cert when acquisition completes.  Adding an mDNS
`.local` domain is active immediately (served over http).  All changes update the active
config (so routing sees them) and regenerate + restart Caddy (so it terminates/serves them).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import closing

import attr
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.background_tasks import BackgroundTask
from litestar.background_tasks import BackgroundTasks
from litestar.di import NamedDependency
from litestar.enums import MediaType
from litestar.exceptions import NotFoundException
from litestar.exceptions import ValidationException
from litestar.params import FromPath

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.caddy import reload_caddy_for_domains
from compute_space.core.dns.coredns_provider.interface import DnsNotBoundError
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.domains import AppsBusyForPrimaryChangeError
from compute_space.core.domains import ArchiveMigrationInProgressError
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainNotFoundError
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import PrimaryDomainChangedError
from compute_space.core.domains import effective_domains
from compute_space.core.domains import get_record
from compute_space.core.domains import load_records
from compute_space.core.domains import remove_non_primary_record
from compute_space.core.domains import set_primary_domain
from compute_space.core.domains import set_record_status
from compute_space.core.domains import upsert_record
from compute_space.core.logging import logger
from compute_space.core.tls.domain_certs import ensure_cert_for
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status
from compute_space.core.updates import is_shutdown_pending
from compute_space.core.updates import trigger_restart
from compute_space.db import get_db
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.exceptions import ConflictException

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
class SetPrimaryDomainRequest:
    expected_primary: str


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
    config: Config, name: str, record: DomainRecord | None, db: sqlite3.Connection
) -> tuple[DomainCertStatus, str | None]:
    """Cert status for a TLS domain, derived from the cert actually on disk (what Caddy serves) so an
    expired/unreadable cert is never shown 'active'; falls back to the stored in-flight state."""
    cert_path, key_path = config.cert_key_paths_for(db, name)
    status = get_cert_status(cert_path, key_path)
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


def _domain_info(config: Config, domain: Domain, record: DomainRecord | None, db: sqlite3.Connection) -> DomainInfo:
    name = domain.name_no_port
    is_primary = record.is_primary if record is not None else False
    if not domain.tls:
        cert_status, error = DomainCertStatus.ACTIVE, None  # http, nothing to acquire
    else:
        cert_status, error = _tls_cert_display(config, name, record, db)
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
    return [_domain_info(config, r.to_domain(), r, db) for r in load_records(db)]


async def _reload_caddy_after_response() -> None:
    """Regenerate + gracefully reload Caddy as a response background task.

    Deferred past the response because the reload falls back to a cold restart when the admin API
    fails, and that *would* drop the request that triggered it.  Reads the *live* config so a
    concurrent domain change isn't dropped from the regenerated Caddyfile."""
    with closing(get_db()) as db:
        await reload_caddy_for_domains(get_config(), db)


async def _restart_after_response() -> None:
    await asyncio.sleep(0.05)
    trigger_restart()


async def _acquire_cert(config: Config, domain: Domain, dns_provider: InternalDnsProvider) -> None:
    """Acquire the domain's cert, then flip its status + reload Caddy so it uses the real cert.
    Runs after the response (acquisition is slow), so it owns one DB connection for the job.
    Records the error on failure."""
    with closing(get_db()) as db:
        try:
            await ensure_cert_for(config, domain, db, dns_provider)
        except Exception as exc:  # noqa: BLE001 — surface any acquisition failure as domain status
            logger.opt(exception=True).error("cert acquisition failed for {}", domain.name)
            set_record_status(db, domain.name_no_port, DomainCertStatus.ERROR, error_message=str(exc))
            return
        set_record_status(db, domain.name_no_port, DomainCertStatus.ACTIVE)
    # Regenerate Caddy from the *live* active config, not the snapshot captured at add time — a
    # domain added while this (slow) acquisition ran would otherwise be dropped from the Caddyfile.
    await _reload_caddy_after_response()


def _validate_new_domain(
    config: Config,
    name: str,
    tls: bool,
    mdns: bool,
    db: sqlite3.Connection,
) -> str | None:
    if not name:
        return "domain name is required"
    if not _DOMAIN_RE.match(name):
        return "invalid domain name"
    if mdns and tls:
        return "Local domains are served over HTTP; set tls=false"
    if any(d.name_no_port == name for d in effective_domains(db)):
        return "domain is already configured"
    return None


@get("/api/domains", guards=[require_owner_auth])
async def list_domains(config: NamedDependency[Config], db: NamedDependency[sqlite3.Connection]) -> DomainListResponse:
    return DomainListResponse(domains=_domain_list(config, db))


@post("/api/domains", status_code=202, guards=[require_owner_auth], raises=[ValidationException])
async def add_domain(
    data: AddDomainRequest,
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
    dns_provider: NamedDependency[InternalDnsProvider],
) -> Response[DomainListResponse]:
    name = data.name.strip().lower()
    error = _validate_new_domain(config, name, data.tls, data.mdns, db)
    if error is not None:
        raise ValidationException(detail=error)

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
        # Make CoreDNS authoritative before DNS-01 acquisition starts.
        try:
            await dns_provider.add_zone(name)
        except DnsNotBoundError:
            logger.warning("Added {} but this instance is not serving DNS for it", name)
    background: list[BackgroundTask] = [BackgroundTask(_reload_caddy_after_response)]
    if data.tls:
        background.append(BackgroundTask(_acquire_cert, config, domain, dns_provider))
    return Response(
        DomainListResponse(domains=_domain_list(config, db)),
        status_code=202,
        media_type=MediaType.JSON,
        background=BackgroundTasks(background),
    )


@post(
    "/api/domains/{name:str}/primary",
    status_code=200,
    guards=[require_owner_auth],
    raises=[NotFoundException, ConflictException],
)
async def make_primary_domain(
    name: FromPath[str],
    data: SetPrimaryDomainRequest,
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
) -> Response[DomainListResponse]:
    """Promote an existing ready domain, then restart through the standard startup path."""
    if is_shutdown_pending():
        raise ConflictException(detail="the instance is already restarting", extra={"code": "restart_pending"})
    name = name.strip().lower()
    record = get_record(db, name)
    if record is None:
        raise NotFoundException(detail="domain not found")
    if record.tls:
        cert_path, key_path = config.cert_key_paths_for(db, name)
        if get_cert_status(cert_path, key_path) not in (CertStatus.OK, CertStatus.EXPIRING_SOON):
            raise ConflictException(
                detail="domain must have a usable TLS certificate before it can be made primary",
                extra={"code": "domain_not_ready"},
            )
    try:
        changed = set_primary_domain(db, name, data.expected_primary)
    except DomainNotFoundError as exc:
        raise NotFoundException(detail="domain not found") from exc
    except PrimaryDomainChangedError as exc:
        raise ConflictException(
            detail=f"primary domain changed to {exc.current_primary}; reload and try again",
            extra={"code": "primary_changed", "current_primary": exc.current_primary},
        ) from exc
    except AppsBusyForPrimaryChangeError as exc:
        raise ConflictException(
            detail="wait for active app operations to finish before changing the primary domain",
            extra={"code": "apps_busy", "apps": list(exc.app_names)},
        ) from exc
    except ArchiveMigrationInProgressError as exc:
        raise ConflictException(
            detail="wait for the archive migration to finish before changing the primary domain",
            extra={"code": "archive_busy"},
        ) from exc

    return Response(
        DomainListResponse(domains=_domain_list(config, db)),
        status_code=200,
        media_type=MediaType.JSON,
        background=BackgroundTask(_restart_after_response) if changed else None,
    )


@delete(
    "/api/domains/{name:str}",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException],
)
async def remove_domain(
    name: FromPath[str],
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
    dns_provider: NamedDependency[InternalDnsProvider],
) -> Response[DomainListResponse]:
    name = name.strip().lower()
    removed = get_record(db, name)
    if removed is None:
        raise NotFoundException(detail="domain not found")
    if not remove_non_primary_record(db, removed.name):
        current = get_record(db, name)
        if current is not None and current.is_primary:
            raise ValidationException(detail="cannot remove the primary domain")
        raise NotFoundException(detail="domain not found")
    if removed is not None and not removed.mdns and dns_provider.serves_public_zones:
        # Drop the zone from CoreDNS so it stops answering for the removed public domain, and
        # discard its zone file.  The records survive — they belong to no zone.
        await dns_provider.remove_zone(name)
    # Regenerate Caddy only after this response has been sent — see _reload_caddy_after_response.
    return Response(
        DomainListResponse(domains=_domain_list(config, db)),
        status_code=200,
        media_type=MediaType.JSON,
        background=BackgroundTask(_reload_caddy_after_response),
    )


api_domains_routes = Router(path="/", route_handlers=[list_domains, add_domain, make_primary_domain, remove_domain])
