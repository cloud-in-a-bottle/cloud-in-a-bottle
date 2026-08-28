from __future__ import annotations

import sqlite3

from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version

from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.app_id import ROUTER_APP_NAME
from compute_space.core.proxy_target import InProcess
from compute_space.core.proxy_target import LocalPort
from compute_space.core.service_interface.builtin_services import builtin_for
from compute_space.core.service_interface.provider import NoProviderError
from compute_space.core.service_interface.provider import ProviderNotRunningError
from compute_space.core.service_interface.provider import ProviderUnavailable
from compute_space.core.service_interface.provider import ProviderVersionError
from compute_space.core.service_interface.provider import ResolvedProvider


def resolve_provider(
    service_url: str,
    version_specifier: str,
    db: sqlite3.Connection,
    provider_app_id: str | None = None,
) -> ResolvedProvider:
    """Pick the provider for a service, whether the router serves it or an app does.

    Builtins are checked first; one yields to an app the owner has made the default (see
    ``builtin_for``).  ``provider_app_id`` pins a specific provider instead — ``ROUTER_APP_ID`` to
    demand the builtin — and fails rather than falling back if that one can't serve.

    Raises a :class:`ProviderUnavailable` subclass for every "can't serve this" case, so callers
    can map the lot to one status code without matching on message text.
    """
    try:
        spec = SpecifierSet(version_specifier)
    except InvalidSpecifier as e:
        raise ProviderUnavailable(f"Invalid version specifier: {version_specifier}") from e

    builtin = builtin_for(service_url, db, provider_override=provider_app_id)
    if builtin is not None:
        _check_version(builtin.version, spec, version_specifier, ROUTER_APP_NAME)
        return ResolvedProvider(
            service_url=service_url,
            app_id=ROUTER_APP_ID,
            name=ROUTER_APP_NAME,
            version=builtin.version,
            endpoint="/",
            target=InProcess(builtin.app),
        )

    target_app_id = provider_app_id or _default_provider_id(service_url, db)
    row = db.execute(
        """SELECT sp.service_version, sp.endpoint, a.local_port, a.status, a.name
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ? AND sp.app_id = ?""",
        (service_url, target_app_id),
    ).fetchone()
    if not row:
        raise NoProviderError(f"Provider '{target_app_id}' not found for service '{service_url}'")
    if row["status"] != "running":
        raise ProviderNotRunningError(f"Provider '{row['name']}' for '{service_url}' is not running")
    _check_version(row["service_version"], spec, version_specifier, row["name"])

    return ResolvedProvider(
        service_url=service_url,
        app_id=target_app_id,
        name=row["name"],
        version=row["service_version"],
        endpoint=row["endpoint"],
        target=LocalPort(row["local_port"]),
    )


def _default_provider_id(service_url: str, db: sqlite3.Connection) -> str:
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (service_url,)).fetchone()
    if not row:
        raise NoProviderError(f"No provider for service '{service_url}'")
    return str(row["app_id"])


def _check_version(provider_version: str, spec: SpecifierSet, specifier_text: str, provider_name: str) -> None:
    try:
        version = Version(provider_version)
    except InvalidVersion as e:
        raise ProviderVersionError(f"Provider '{provider_name}' has invalid version '{provider_version}'") from e
    if version not in spec:
        raise ProviderVersionError(
            f"Provider '{provider_name}' version {provider_version} does not match '{specifier_text}'"
        )
