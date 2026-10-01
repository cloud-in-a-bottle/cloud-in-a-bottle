import sqlite3

from litestar import Router
from litestar import get
from litestar.di import NamedDependency
from litestar.response import Template

from compute_space.core.archive_backend import read_state
from compute_space.core.managed_storage import ManagedStorageError
from compute_space.core.managed_storage import active_binding
from compute_space.web.auth.auth import require_owner_auth


@get("/settings", guards=[require_owner_auth])
async def settings_page(db: NamedDependency[sqlite3.Connection]) -> Template:
    # Bootstrap the usage panel without waiting for archive metadata listing,
    # which can fail or stall when object storage itself is unavailable.
    error = None
    try:
        binding = active_binding(db, read_state(db))
    except ManagedStorageError as exc:
        binding = None
        error = str(exc)
    return Template(
        template_name="settings.html",
        context={
            "managed_storage_allocation_id": binding.allocation_id if binding else None,
            "managed_storage_error": error,
        },
    )


@get("/updating", guards=[require_owner_auth])
async def updating_page() -> Template:
    return Template(template_name="updating.html")


pages_settings_routes = Router(path="/", route_handlers=[settings_page, updating_page])
