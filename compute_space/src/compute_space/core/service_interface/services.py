import sqlite3

from packaging.version import Version

from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.service_interface import builtin_services
from compute_space.core.service_interface.provider import ServiceProvider
from compute_space.db.connection import make_atomic_with_savepoint


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
    """Sync the set of services this app provides to match its manifest.

    Registering says only what an app *can* serve, never what it *does* serve: a row in
    ``service_defaults`` means the owner chose that provider, and nothing else may write one.
    This runs on every install, start and reload, so a registration that also claimed the default
    would silently undo the owner's choice the next time the app booted.
    """
    with make_atomic_with_savepoint(db):
        db.execute("DELETE FROM service_providers_v2 WHERE app_id = ?", (app_id,))
        for svc in manifest.provides_services_v2:
            db.execute(
                "INSERT OR REPLACE INTO service_providers_v2 (service_url, app_id, service_version, endpoint) VALUES (?, ?, ?, ?)",
                (svc.service, app_id, svc.version, svc.endpoint),
            )


def list_all_service_providers(db: sqlite3.Connection, service_url: str | None = None) -> list[ServiceProvider]:
    """Every provider of every service, builtins included — or of one service, if named."""
    all_rows = db.execute(
        """SELECT sp.service_url, sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    rows = [r for r in all_rows if service_url in (None, r["service_url"])]
    builtins = [b for b in builtin_services.BUILTIN_SERVICES if service_url in (None, b.service_url)]

    service_urls = {r["service_url"] for r in rows} | {b.service_url for b in builtins}
    defaults = {url: default_provider_id_for_service(url, db) for url in service_urls}
    return [
        builtin_services.builtin_as_provider(b, is_default=defaults.get(b.service_url) == ROUTER_APP_ID)
        for b in builtins
    ] + [
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


def default_provider_id_for_service(service_url: str, db: sqlite3.Connection) -> str | None:
    """Which provider serves this service by default?

    In priority order:
    1. the app the owner picked;
    2. the router's builtin, which holds the service until an app is picked and takes it back when
       the owner clears that;
    3. the incumbent app otherwise — a service keeps working when its default app is uninstalled
       (the default row cascades away with it) or was never chosen.

    Only 1 is stored; 2 and 3 are derived, so nothing has to be written to keep a service served.
    Returns None only if nothing provides the service at all.
    ROUTER_APP_ID is never stored in service_defaults — the column is a foreign key into apps.
    """
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (service_url,)).fetchone()
    if row:
        return str(row["app_id"])
    if builtin_services.builtin_by_url(service_url) is not None:
        return ROUTER_APP_ID
    return _incumbent_provider_id(service_url, db)


def _incumbent_provider_id(service_url: str, db: sqlite3.Connection) -> str | None:
    """Of the apps providing a service nobody has chosen between, the one installed longest ago.

    Two providers of a service are not interchangeable — each holds its own data — so installing
    a second one must not move the service off the first.  Version breaks a tie only between apps
    installed in the same second: it says which revision of the spec an app implements, nothing
    about which app has the data.
    """
    rows = db.execute(
        """SELECT sp.app_id, sp.service_version, a.created_at
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ?""",
        (service_url,),
    ).fetchall()
    if not rows:
        return None
    oldest = min(r["created_at"] for r in rows)
    tied = [r for r in rows if r["created_at"] == oldest]
    # Versions are validated when the manifest is parsed, so they are all comparable here.
    return str(max(tied, key=lambda r: (Version(r["service_version"]), r["app_id"]))["app_id"])


def set_default(service_url: str, app_id: str, db: sqlite3.Connection) -> None:
    """Point a service at a provider.  Raises LookupError if it doesn't provide that service.

    Picking the router is stored as the *absence* of a row rather than one naming it:
    ``service_defaults.app_id`` is a foreign key into ``apps`` and the router has none.
    ``default_provider_id_for_service`` reads it back the same way, so callers can pass
    ``ROUTER_APP_ID`` here like any other provider id and never see the difference.
    """
    if app_id == ROUTER_APP_ID:
        if builtin_services.builtin_by_url(service_url) is None:
            raise LookupError(f"The router does not provide '{service_url}'")
        clear_default(service_url, db)
        return

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
