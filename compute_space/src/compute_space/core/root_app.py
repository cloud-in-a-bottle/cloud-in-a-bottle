from __future__ import annotations

import sqlite3
from enum import StrEnum

import attr

from compute_space.core.app_id import is_valid_app_name
from compute_space.core.domains import PRIMARY_DOMAIN_RESTART_MARKER
from compute_space.core.domains import Domain
from compute_space.core.domains import effective_domains
from compute_space.core.settings_store import get_setting

# Where external clients look up the router's URL (see ``web.routes.api.discovery``).
DISCOVERY_PATH = "/.well-known/cloud-in-a-bottle"

# Paths on the bare domain that the router keeps whatever is served there.  Exact matches only, so the rest of
# ``/.well-known/`` belongs to the root app, if there is one.
ROUTER_ROOT_PATHS = frozenset({"/.well-known/openhost-identity", "/.well-known/jwks.json", DISCOVERY_PATH})

DEFAULT_ROUTER_SUBDOMAIN = "bottle"

_KIND_KEY = "root_kind"
_APP_ID_KEY = "root_app_id"
_ROUTER_SUBDOMAIN_KEY = "router_subdomain"


class RootKind(StrEnum):
    """What the bare domain serves."""

    DASHBOARD = "dashboard"  # the router: dashboard, API, login.  The default.
    NOTHING = "nothing"
    APP = "app"


@attr.s(auto_attribs=True, frozen=True)
class RootSetting:
    """What the bare domain serves, and so where the router lives.  Global across every configured domain."""

    kind: RootKind = RootKind.DASHBOARD
    # The app served at the bare domain (as well as at its own subdomain); set iff ``kind`` is APP.
    app_id: str | None = None
    # The subdomain the router lives on while it isn't at the bare domain.  Kept while it is, so the owner's choice
    # survives switching back and forth.
    router_subdomain: str = DEFAULT_ROUTER_SUBDOMAIN

    @property
    def router_label(self) -> str | None:
        """The router's subdomain label, or None while the router is at the bare domain."""
        return None if self.kind == RootKind.DASHBOARD else self.router_subdomain

    def router_host(self, zone: Domain) -> str:
        """The host serving the router on ``zone``, without a port."""
        label = self.router_label
        return zone.name_no_port if label is None else f"{label}.{zone.name_no_port}"

    def router_url(self, zone: Domain) -> str:
        """Canonical router URL on ``zone``, keeping a port baked into its configured name."""
        label = self.router_label
        return zone.url if label is None else f"{zone.scheme}://{label}.{zone.name}"

    def is_router_host(self, zone: Domain, host: str) -> bool:
        return host.split(":")[0].lower() == self.router_host(zone)


class RootSettingError(ValueError):
    pass


class RootAppNotFoundError(RootSettingError):
    pass


class AppsBusyForDashboardMoveError(RuntimeError):
    def __init__(self, app_names: tuple[str, ...]) -> None:
        super().__init__(f"apps are busy: {', '.join(app_names)}")
        self.app_names = app_names


def load_root_setting(db: sqlite3.Connection) -> RootSetting:
    kind = RootKind(get_setting(db, _KIND_KEY) or RootKind.DASHBOARD)
    app_id = get_setting(db, _APP_ID_KEY) if kind == RootKind.APP else None
    if kind == RootKind.APP and app_id is None:
        raise RuntimeError("root_kind is 'app' but no root_app_id is set")
    return RootSetting(
        kind=kind, app_id=app_id, router_subdomain=get_setting(db, _ROUTER_SUBDOMAIN_KEY) or DEFAULT_ROUTER_SUBDOMAIN
    )


def is_router_subdomain(db: sqlite3.Connection, name: str) -> bool:
    """True if ``name`` is the router's current subdomain, which no app may take."""
    return load_root_setting(db).router_label == name


def validate_root_setting(db: sqlite3.Connection, setting: RootSetting) -> None:
    if setting.kind == RootKind.APP:
        if setting.app_id is None:
            raise RootSettingError("Choose an app to serve at the bare domain")
        row = db.execute("SELECT 1 FROM apps WHERE app_id = ? AND status != 'removing'", (setting.app_id,)).fetchone()
        if row is None:
            raise RootAppNotFoundError(setting.app_id)
    elif setting.app_id is not None:
        raise RootSettingError("An app can only be given when one is served at the bare domain")

    label = setting.router_subdomain
    if not is_valid_app_name(label):
        raise RootSettingError(
            "The dashboard subdomain must be lowercase alphanumeric (hyphens allowed, not at start/end)"
        )
    if setting.router_label is None:
        return
    if db.execute("SELECT 1 FROM apps WHERE name = ?", (label,)).fetchone() is not None:
        raise RootSettingError(f"An app is already served at the '{label}' subdomain")
    names = {d.name_no_port for d in effective_domains(db)}
    if clash := sorted(name for name in names if name.split(".", 1)[0] == label and name.split(".", 1)[-1] in names):
        raise RootSettingError(f"{clash[0]} is configured as a domain of its own")


def apply_root_setting(db: sqlite3.Connection, setting: RootSetting) -> bool:
    """Validate and store ``setting``.  Returns True if the router moved, in which case running apps have been marked
    for restart (their ``OPENHOST_ROUTER_PUBLIC_URL`` changed) and the caller must restart the router, the same way
    a primary-domain change does."""
    db.execute("BEGIN IMMEDIATE")
    try:
        validate_root_setting(db, setting)
        moved = load_root_setting(db).router_label != setting.router_label
        if moved:
            busy_apps = tuple(
                row["name"]
                for row in db.execute(
                    "SELECT name FROM apps WHERE status IN ('building', 'starting', 'removing') ORDER BY name"
                )
            )
            if busy_apps:
                raise AppsBusyForDashboardMoveError(busy_apps)
            db.execute(
                "UPDATE apps SET status = 'starting', error_message = ? "
                "WHERE status = 'running' OR (status = 'error' AND container_id IS NOT NULL)",
                (PRIMARY_DOMAIN_RESTART_MARKER,),
            )
        upsert = (
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        db.execute(upsert, (_KIND_KEY, setting.kind))
        db.execute(upsert, (_ROUTER_SUBDOMAIN_KEY, setting.router_subdomain))
        if setting.app_id is None:
            db.execute("DELETE FROM settings WHERE key = ?", (_APP_ID_KEY,))
        else:
            db.execute(upsert, (_APP_ID_KEY, setting.app_id))
        db.execute("COMMIT")
        return moved
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def clear_root_app_if(db: sqlite3.Connection, app_id: str) -> None:
    """Stop serving ``app_id`` at the bare domain, leaving it empty; the router stays where it is, so apps needn't
    restart.  Doesn't commit, so it can share a transaction with the app's removal."""
    if db.execute("SELECT 1 FROM settings WHERE key = ? AND value = ?", (_APP_ID_KEY, app_id)).fetchone() is None:
        return
    db.execute("UPDATE settings SET value = ? WHERE key = ?", (RootKind.NOTHING, _KIND_KEY))
    db.execute("DELETE FROM settings WHERE key = ?", (_APP_ID_KEY,))
