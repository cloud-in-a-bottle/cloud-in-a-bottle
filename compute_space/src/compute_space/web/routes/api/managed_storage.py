import sqlite3

import attr
from litestar import Response
from litestar import get
from litestar.di import NamedDependency

from compute_space.config import Config
from compute_space.core.archive_backend import read_state
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.managed_storage import ManagedStatus
from compute_space.core.managed_storage import ManagedStorageBinding
from compute_space.core.managed_storage import ManagedStorageError
from compute_space.core.managed_storage import active_binding
from compute_space.core.managed_storage import fetch_status
from compute_space.web.auth.auth import require_owner_auth


@attr.s(auto_attribs=True, frozen=True)
class ManagedUsageResponse:
    managed: bool
    status: ManagedStatus | None = None
    error: str | None = None


_HEADERS = {"Cache-Control": "private, no-store"}
_CHANGED = "Storage configuration changed. Reload settings to view its usage."


def _respond(body: ManagedUsageResponse, status_code: int = 200) -> Response[ManagedUsageResponse]:
    return Response(body, status_code=status_code, headers=_HEADERS)


def _binding_moved(db: sqlite3.Connection, binding: ManagedStorageBinding) -> Response[ManagedUsageResponse] | None:
    """The binding can change while the upstream request is in flight. A result, or a failure, only describes the
    binding it was fetched for."""
    current = active_binding(db, read_state(db))
    if current is None:
        return _respond(ManagedUsageResponse(managed=False))
    if current != binding:
        return _respond(ManagedUsageResponse(managed=True, error=_CHANGED), 409)
    return None


@get("/api/storage/managed_usage", guards=[require_owner_auth])
async def managed_usage(
    db: NamedDependency[sqlite3.Connection], config: NamedDependency[Config]
) -> Response[ManagedUsageResponse]:
    try:
        binding = active_binding(db, read_state(db))
        if binding is None:
            return _respond(ManagedUsageResponse(managed=False))
        try:
            identity = get_instance_identity(db, config)
            if identity is None:
                raise ManagedStorageError("Connect this instance to Imbue to view cloud storage usage.")
            status = await fetch_status(binding, identity)
        except ManagedStorageError as exc:
            return _binding_moved(db, binding) or _respond(ManagedUsageResponse(managed=True, error=str(exc)), 503)
        return _binding_moved(db, binding) or _respond(ManagedUsageResponse(managed=True, status=status))
    except ManagedStorageError as exc:
        return _respond(ManagedUsageResponse(managed=True, error=str(exc)), 503)
