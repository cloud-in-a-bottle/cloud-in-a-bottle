import sqlite3

import attr
from litestar import Response
from litestar import get
from litestar.di import NamedDependency

from compute_space.config import Config
from compute_space.core.archive_backend import read_state
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.managed_storage import ManagedStatus
from compute_space.core.managed_storage import ManagedStorageError
from compute_space.core.managed_storage import active_binding
from compute_space.core.managed_storage import fetch_status
from compute_space.web.auth.auth import require_owner_auth


@attr.s(auto_attribs=True, frozen=True)
class ManagedUsageResponse:
    managed: bool
    status: ManagedStatus | None = None
    error: str | None = None


@get("/api/storage/managed_usage", guards=[require_owner_auth])
async def managed_usage(
    db: NamedDependency[sqlite3.Connection], config: NamedDependency[Config]
) -> Response[ManagedUsageResponse]:
    headers = {"Cache-Control": "private, no-store"}
    try:
        binding = active_binding(db, read_state(db))
        if binding is None:
            return Response(ManagedUsageResponse(managed=False), headers=headers)
        identity = get_instance_identity(db, config)
        if identity is None:
            raise ManagedStorageError("Connect this instance to Imbue to view cloud storage usage.")
        status = await fetch_status(binding, identity)
        if active_binding(db, read_state(db)) != binding:
            raise ManagedStorageError("Storage configuration changed. Reload settings to view its usage.")
        return Response(ManagedUsageResponse(managed=True, status=status), headers=headers)
    except ManagedStorageError as exc:
        return Response(ManagedUsageResponse(managed=True, error=str(exc)), status_code=503, headers=headers)
