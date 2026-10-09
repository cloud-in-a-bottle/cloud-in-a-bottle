from __future__ import annotations

import sqlite3

from compute_space.core.settings_store import delete_setting
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

# The app served at the bare domain (as well as at its own subdomain).  Unset means the router (dashboard) is
# served there, which is the default.  Global across every configured domain.
ROOT_APP_ID_KEY = "root_app_id"

# Paths on the bare domain that the router keeps even while an app is served there.  Exact matches only, so the
# rest of ``/.well-known/`` still belongs to the app.
ROUTER_ROOT_PATHS = frozenset({"/.well-known/openhost-identity", "/.well-known/jwks.json"})


class RootAppNotFoundError(ValueError):
    pass


def get_root_app_id(db: sqlite3.Connection) -> str | None:
    return get_setting(db, ROOT_APP_ID_KEY)


def set_root_app_id(db: sqlite3.Connection, app_id: str | None) -> None:
    """Serve ``app_id`` at the bare domain, or the router if None."""
    if app_id is None:
        delete_setting(db, ROOT_APP_ID_KEY)
        return
    row = db.execute("SELECT 1 FROM apps WHERE app_id = ? AND status != 'removing'", (app_id,)).fetchone()
    if row is None:
        raise RootAppNotFoundError(app_id)
    set_setting(db, ROOT_APP_ID_KEY, app_id)


def clear_root_app_if(db: sqlite3.Connection, app_id: str) -> None:
    """Hand the bare domain back to the router if ``app_id`` is served there.  Doesn't commit, so it can share a
    transaction with the app's removal."""
    db.execute("DELETE FROM settings WHERE key = ? AND value = ?", (ROOT_APP_ID_KEY, app_id))
