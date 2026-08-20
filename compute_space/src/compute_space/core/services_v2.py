import sqlite3

from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version

from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db.connection import make_atomic_with_savepoint


def lookup_shortname(consumer_app_id: str, shortname: str, db: sqlite3.Connection) -> tuple[str, str]:
    """Resolve (service_url, version_spec) by shortname from the consumer's stored manifest.

    Manifest is read from apps.manifest_raw and parsed on each call (typically a few KB of TOML).
    """
    row = db.execute("SELECT manifest_raw FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    if not row or not row["manifest_raw"]:
        raise LookupError(f"No manifest stored for app '{consumer_app_id}'")
    manifest = parse_manifest_from_string(row["manifest_raw"])
    for perm in manifest.consumes_services_v2:
        if perm.shortname == shortname:
            return perm.service, perm.version
    raise LookupError(f"Shortname '{shortname}' not declared in '{consumer_app_id}' manifest")


def register_v2_service_providers(
    app_id: str,
    manifest: AppManifest,
    db: sqlite3.Connection,
) -> None:
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


def resolve_provider(
    service_url: str,
    version_specifier: str,
    db: sqlite3.Connection,
    provider_app_id: str | None = None,
) -> tuple[str, int, str, str]:
    """Resolve a provider for a service URL, checking version compatibility.

    Returns (app_id, local_port, version, endpoint).

    If provider_app_id is given, that specific app is used (fail if it doesn't exist or is version-incompatible).
    Otherwise the default provider is used (same semantics - fail if it doesn't exist or is version-incompatible).
    """
    try:
        spec = SpecifierSet(version_specifier)
    except InvalidSpecifier as e:
        raise RuntimeError(f"Invalid version specifier: {version_specifier}") from e

    if provider_app_id:
        target_app_id = provider_app_id
    else:
        default = db.execute(
            "SELECT app_id FROM service_defaults WHERE service_url = ?",
            (service_url,),
        ).fetchone()
        if not default:
            raise RuntimeError(f"No provider for service '{service_url}'")
        target_app_id = default["app_id"]

    row = db.execute(
        """SELECT sp.service_version, sp.endpoint, a.local_port, a.status, a.name
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ? AND sp.app_id = ?""",
        (service_url, target_app_id),
    ).fetchone()

    if not row:
        raise RuntimeError(f"Provider '{target_app_id}' not found for service '{service_url}'")

    if row["status"] != "running":
        raise RuntimeError(f"Provider '{row['name']}' for '{service_url}' is not running")

    try:
        v = Version(row["service_version"])
    except InvalidVersion as e:
        raise RuntimeError(f"Provider '{row['name']}' has invalid version '{row['service_version']}'") from e

    if v not in spec:
        raise RuntimeError(
            f"Provider '{row['name']}' version {row['service_version']} does not match '{version_specifier}'"
        )

    return target_app_id, row["local_port"], row["service_version"], row["endpoint"]
