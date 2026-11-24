import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import attr
from litestar import Request
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar import put
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.exceptions import ValidationException
from litestar.params import FromPath

from compute_space.core.auth.invites import AppUser
from compute_space.core.auth.invites import app_users
from compute_space.core.auth.invites import create_invite
from compute_space.core.auth.invites import invite_apps
from compute_space.core.auth.invites import set_app_users
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.helpers.zone import router_base_url


@attr.s(auto_attribs=True, frozen=True)
class InvitedUser:
    user_id: int
    username: str
    created_at: str


@attr.s(auto_attribs=True, frozen=True)
class Invite:
    id: int
    name: str
    apps: list[str]
    uses_left: int
    expires_at: str
    expired: bool
    users: list[InvitedUser]


@attr.s(auto_attribs=True, frozen=True)
class CreateInviteRequest:
    name: str
    app_ids: list[str]
    max_uses: int
    expiry_hours: float


@attr.s(auto_attribs=True, frozen=True)
class SetAppUsersRequest:
    user_ids: list[int]


@attr.s(auto_attribs=True, frozen=True)
class CreatedInvite:
    id: int
    url: str


@get("/api/invites", guards=[require_owner_auth])
async def invites_list(db: NamedDependency[sqlite3.Connection]) -> list[Invite]:
    now = datetime.now(UTC)
    invites = []
    for row in db.execute("SELECT * FROM invites ORDER BY created_at DESC, id DESC").fetchall():
        users = db.execute(
            "SELECT user_id, username, created_at FROM users WHERE invite_id = ? ORDER BY user_id", (row["id"],)
        )
        invites.append(
            Invite(
                id=row["id"],
                name=row["name"],
                apps=[a.name for a in invite_apps(db, row["id"])],
                uses_left=row["uses_left"],
                expires_at=row["expires_at"],
                expired=datetime.fromisoformat(row["expires_at"]) <= now,
                users=[
                    InvitedUser(user_id=m["user_id"], username=m["username"], created_at=m["created_at"])
                    for m in users
                ],
            )
        )
    return invites


@post("/api/invites", status_code=200, guards=[require_owner_auth], raises=[ValidationException])
async def invites_create(
    request: Request[Any, Any, Any], data: CreateInviteRequest, db: NamedDependency[sqlite3.Connection]
) -> CreatedInvite:
    app_ids = sorted(set(data.app_ids))
    if not app_ids:
        raise ValidationException(detail="Choose at least one app")
    placeholders = ",".join("?" * len(app_ids))
    query = f"SELECT COUNT(*) FROM apps WHERE user_paths != '[]' AND app_id IN ({placeholders})"
    if db.execute(query, app_ids).fetchone()[0] != len(app_ids):
        raise ValidationException(detail="Only apps that declare user_paths can be shared")
    if data.max_uses < 1:
        raise ValidationException(detail="Max uses must be at least 1")
    if data.expiry_hours <= 0:
        raise ValidationException(detail="Expiry must be positive")
    expires_at = datetime.now(UTC) + timedelta(hours=data.expiry_hours)
    invite_id, token = create_invite(db, data.name.strip() or "Untitled", app_ids, data.max_uses, expires_at)
    db.commit()
    return CreatedInvite(id=invite_id, url=f"{router_base_url(request)}/invite/{token}")


@post("/api/invites/{invite_id:int}/close", status_code=204, guards=[require_owner_auth])
async def invites_close(invite_id: FromPath[int], db: NamedDependency[sqlite3.Connection]) -> None:
    db.execute("UPDATE invites SET uses_left = 0 WHERE id = ?", (invite_id,))
    db.commit()


@delete("/api/invites/{invite_id:int}", guards=[require_owner_auth])
async def invites_delete(invite_id: FromPath[int], db: NamedDependency[sqlite3.Connection]) -> None:
    db.execute("DELETE FROM invites WHERE id = ?", (invite_id,))
    db.commit()


@delete("/api/users/{user_id:int}", guards=[require_owner_auth])
async def users_delete(user_id: FromPath[int], db: NamedDependency[sqlite3.Connection]) -> None:
    db.execute("DELETE FROM users WHERE user_id = ? AND invite_id IS NOT NULL", (user_id,))
    db.commit()


@put("/api/apps/{app_id:str}/users", guards=[require_owner_auth], raises=[ValidationException, NotFoundException])
async def app_users_set(
    app_id: FromPath[str], data: SetAppUsersRequest, db: NamedDependency[sqlite3.Connection]
) -> list[AppUser]:
    """Set which users are assigned to this app directly. Users whose invite shares it keep access regardless."""
    row = db.execute("SELECT user_paths FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if row is None:
        raise NotFoundException(detail="App not found")
    if row["user_paths"] == "[]":
        raise ValidationException(detail="Only apps that declare user_paths can be shared")
    user_ids = sorted(set(data.user_ids))
    placeholders = ",".join("?" * len(user_ids))
    query = f"SELECT COUNT(*) FROM users WHERE invite_id IS NOT NULL AND user_id IN ({placeholders})"
    if user_ids and db.execute(query, user_ids).fetchone()[0] != len(user_ids):
        raise ValidationException(detail="Unknown user")
    set_app_users(db, app_id, user_ids)
    db.commit()
    return app_users(db, app_id)


api_invites_routes = Router(
    path="/",
    route_handlers=[invites_list, invites_create, invites_close, invites_delete, users_delete, app_users_set],
)
