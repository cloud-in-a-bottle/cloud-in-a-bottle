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
from litestar.di import NamedDependency
from litestar.exceptions import ValidationException
from litestar.params import FromPath

from compute_space.core.auth.invites import create_invite
from compute_space.core.auth.invites import invite_app_names
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.helpers.zone import router_base_url


@attr.s(auto_attribs=True, frozen=True)
class Member:
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
    members: list[Member]


@attr.s(auto_attribs=True, frozen=True)
class CreateInviteRequest:
    name: str
    app_ids: list[str]
    max_uses: int
    expiry_hours: float


@attr.s(auto_attribs=True, frozen=True)
class CreatedInvite:
    id: int
    url: str


@get("/api/invites", guards=[require_owner_auth])
async def invites_list(db: NamedDependency[sqlite3.Connection]) -> list[Invite]:
    now = datetime.now(UTC)
    invites = []
    for row in db.execute("SELECT * FROM invites ORDER BY created_at DESC, id DESC").fetchall():
        members = db.execute(
            "SELECT user_id, username, created_at FROM users WHERE invite_id = ? ORDER BY user_id", (row["id"],)
        )
        invites.append(
            Invite(
                id=row["id"],
                name=row["name"],
                apps=invite_app_names(db, row["id"]),
                uses_left=row["uses_left"],
                expires_at=row["expires_at"],
                expired=datetime.fromisoformat(row["expires_at"]) <= now,
                members=[
                    Member(user_id=m["user_id"], username=m["username"], created_at=m["created_at"]) for m in members
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
    known = db.execute(f"SELECT COUNT(*) FROM apps WHERE app_id IN ({placeholders})", app_ids).fetchone()[0]
    if known != len(app_ids):
        raise ValidationException(detail="Unknown app")
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


@delete("/api/members/{user_id:int}", guards=[require_owner_auth])
async def members_delete(user_id: FromPath[int], db: NamedDependency[sqlite3.Connection]) -> None:
    db.execute("DELETE FROM users WHERE user_id = ? AND invite_id IS NOT NULL", (user_id,))
    db.commit()


api_invites_routes = Router(
    path="/",
    route_handlers=[invites_list, invites_create, invites_close, invites_delete, members_delete],
)
