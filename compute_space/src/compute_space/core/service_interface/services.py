import sqlite3

import attr

from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.service_interface import builtin_services
from compute_space.db.connection import make_atomic_with_savepoint


@attr.s(auto_attribs=True, frozen=True)
class ServiceProvider:
    service_url: str
    app_id: str
    app_name: str
    service_version: str
    # a subpath of the app's root, e.g. "/v1" or "/api/v2", where the service root lives.
    endpoint: str
    status: str
    is_default: bool


def lookup_service_by_manifest_shortname(
    consumer_app_id: str, shortname: str, db: sqlite3.Connection
) -> tuple[str, str]:
    """Resolve (service_url, version_spec) by shortname from the consumer's stored manifest."""
    row = db.execute("SELECT manifest_raw FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    if not row or not row["manifest_raw"]:
        raise LookupError(f"No manifest stored for app '{consumer_app_id}'")
    manifest = parse_manifest_from_string(row["manifest_raw"])
    for perm in manifest.consumes_services_v2:
        if perm.shortname == shortname:
            return perm.service, perm.version
    raise LookupError(f"Shortname '{shortname}' not declared in '{consumer_app_id}' manifest")


def register_services_provided_by_app(app_id: str, manifest: AppManifest, db: sqlite3.Connection) -> None:
    """Register V2 service providers from manifest. Sets default if none exists."""
    with make_atomic_with_savepoint(db):
        db.execute("DELETE FROM service_providers_v2 WHERE app_id = ?", (app_id,))
        for svc in manifest.provides_services_v2:
            db.execute(
                "INSERT OR REPLACE INTO service_providers_v2 (service_url, app_id, service_version, endpoint) VALUES (?, ?, ?, ?)",
                (svc.service, app_id, svc.version, svc.endpoint),
            )
            existing_default = db.execute(
                "SELECT 1 FROM service_defaults WHERE service_url = ?",
                (svc.service,),
            ).fetchone()
            if not existing_default:
                db.execute(
                    "INSERT INTO service_defaults (service_url, app_id) VALUES (?, ?)",
                    (svc.service, app_id),
                )


def list_all_service_providers(db: sqlite3.Connection) -> list[ServiceProvider]:
    """Every provider of every service, builtins included."""
    rows = db.execute(
        """SELECT sp.service_url, sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    defaults = {d.service_url: d.app_id for d in all_defaults(db)}
    providers = [
        ServiceProvider(
            service_url=r["service_url"],
            app_id=r["app_id"],
            app_name=r["app_name"],
            service_version=r["service_version"],
            endpoint=r["endpoint"],
            status=r["status"],
            is_default=defaults.get(r["service_url"]) == r["app_id"],
        )
        for r in rows
    ]
    builtins = [
        builtin_services.builtin_as_provider(b, is_default=b.service_url not in defaults)
        for b in builtin_services.BUILTIN_SERVICES
    ]
    return builtins + providers


def providers_for(service_url: str, db: sqlite3.Connection) -> list[ServiceProvider]:
    return [p for p in list_all_service_providers(db) if p.service_url == service_url]


def default_provider_id(service_url: str, db: sqlite3.Connection) -> str | None:
    """The app the owner has pointed this service at, or None — which means the router's builtin
    serves it if there is one, and nothing does otherwise."""
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (service_url,)).fetchone()
    return str(row["app_id"]) if row else None


@attr.s(auto_attribs=True, frozen=True)
class ServiceDefault:
    service_url: str
    app_id: str
    app_name: str


def all_defaults(db: sqlite3.Connection) -> list[ServiceDefault]:
    rows = db.execute(
        """SELECT sd.service_url, sd.app_id, a.name AS app_name
           FROM service_defaults sd
           JOIN apps a ON a.app_id = sd.app_id"""
    ).fetchall()
    return [ServiceDefault(service_url=r["service_url"], app_id=r["app_id"], app_name=r["app_name"]) for r in rows]


def set_default(service_url: str, app_id: str, db: sqlite3.Connection) -> None:
    """Point a service at a provider app.  Raises LookupError if that app doesn't provide it."""
    row = db.execute(
        "SELECT 1 FROM service_providers_v2 WHERE service_url = ? AND app_id = ?", (service_url, app_id)
    ).fetchone()
    if not row:
        raise LookupError(f"App '{app_id}' does not provide '{service_url}'")
    db.execute("INSERT OR REPLACE INTO service_defaults (service_url, app_id) VALUES (?, ?)", (service_url, app_id))
    db.commit()


def clear_default(service_url: str, db: sqlite3.Connection) -> None:
    """Un-point a service.  A builtin, if there is one, takes it back over."""
    db.execute("DELETE FROM service_defaults WHERE service_url = ?", (service_url,))
    db.commit()
