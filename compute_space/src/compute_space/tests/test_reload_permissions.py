"""Tests that an app update requiring NEW service permissions is gated on
explicit owner approval, mirroring the approval required at install time.

Before this, ``reload_app_background`` re-synced manifest-derived columns but
silently left newly declared permissions ungranted — the update proceeded and
the new permissions only showed up passively on the app detail page. Now
``_reload_app_impl`` refuses the reload (via ``_gate_update_review``) until
the owner approves, and the app keeps running its current version meanwhile.
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.core.apps import manifest_newly_declared_permissions_v2
from compute_space.core.apps import manifest_settings_changes
from compute_space.core.apps import manifest_ungranted_permissions_v2
from compute_space.core.auth.permissions_v2 import PermissionRecord
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.manifest import parse_manifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.web.routes.api.apps import _gate_update_review
from compute_space.web.routes.api.apps import api_apps_routes

from .conftest import _make_test_config

_CONSUMES = """\
[app]
name = "perm-app"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 5000

{consumes}
"""


def _manifest_with_consumes(repo: Path, consumes: str) -> Any:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "openhost.toml").write_text(_CONSUMES.format(consumes=consumes))
    return parse_manifest(str(repo))


def _consume_block(service: str, shortname: str, grants: str) -> str:
    return (
        "[[services.v2.consumes]]\n"
        f'service = "{service}"\n'
        f'shortname = "{shortname}"\n'
        'version = ">=0.1.0"\n'
        f"grants = [{grants}]\n"
    )


# ─── the shared diff helper ───────────────────────────────────────────────────


def test_ungranted_empty_when_no_consumes(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(tmp_path / "m", "")
    assert manifest_ungranted_permissions_v2(manifest, []) == []


def test_ungranted_lists_declared_when_nothing_granted(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(
        tmp_path / "m", _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )
    ungranted = manifest_ungranted_permissions_v2(manifest, [])
    assert len(ungranted) == 1
    assert ungranted[0].service_url == "github.com/x/secrets"
    assert ungranted[0].grant == {"key": "API_KEY"}


def test_ungranted_excludes_already_granted(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(
        tmp_path / "m", _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/secrets",
            grant={"key": "API_KEY"},
            scope="global",
            provider_app_id=None,
        )
    ]
    assert manifest_ungranted_permissions_v2(manifest, granted) == []


def test_ungranted_grant_match_is_key_order_insensitive(tmp_path: Path) -> None:
    """A stored grant and a declared grant compare equal regardless of dict key
    order (both normalize via json.dumps(sort_keys=True))."""
    manifest = _manifest_with_consumes(
        tmp_path / "m",
        _consume_block("github.com/x/svc", "svc", '{ a = "1", b = "2" }'),
    )
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/svc",
            # Same content, different insertion order.
            grant={"b": "2", "a": "1"},
            scope="global",
            provider_app_id=None,
        )
    ]
    assert manifest_ungranted_permissions_v2(manifest, granted) == []


def test_ungranted_dedups_repeated_declarations(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(
        tmp_path / "m",
        _consume_block("github.com/x/svc", "svc", '{ key = "K" }, { key = "K" }'),
    )
    ungranted = manifest_ungranted_permissions_v2(manifest, [])
    assert len(ungranted) == 1


def test_ungranted_reports_only_the_new_grant(tmp_path: Path) -> None:
    """When one of two declared grants is already held, only the other is new."""
    manifest = _manifest_with_consumes(
        tmp_path / "m",
        _consume_block("github.com/x/svc", "svc", '{ key = "OLD" }, { key = "NEW" }'),
    )
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/svc",
            grant={"key": "OLD"},
            scope="global",
            provider_app_id=None,
        )
    ]
    ungranted = manifest_ungranted_permissions_v2(manifest, granted)
    assert len(ungranted) == 1
    assert ungranted[0].grant == {"key": "NEW"}


def test_ungranted_string_grant(tmp_path: Path) -> None:
    """Grants can be bare strings (e.g. "FULL_ACCESS"), not just tables."""
    manifest = _manifest_with_consumes(tmp_path / "m", _consume_block("github.com/x/svc", "svc", '"FULL_ACCESS"'))
    ungranted = manifest_ungranted_permissions_v2(manifest, [])
    assert len(ungranted) == 1
    assert ungranted[0].grant == "FULL_ACCESS"

    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/svc",
            grant="FULL_ACCESS",
            scope="global",
            provider_app_id=None,
        )
    ]
    assert manifest_ungranted_permissions_v2(manifest, granted) == []


def test_ungranted_spans_multiple_services(tmp_path: Path) -> None:
    """A new permission is detected per-service; a grant held for one service
    does not satisfy a same-named grant declared for a different service."""
    consumes = _consume_block("github.com/x/a", "a", '{ key = "K" }') + _consume_block(
        "github.com/x/b", "b", '{ key = "K" }'
    )
    manifest = _manifest_with_consumes(tmp_path / "m", consumes)
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/a",
            grant={"key": "K"},
            scope="global",
            provider_app_id=None,
        )
    ]
    ungranted = manifest_ungranted_permissions_v2(manifest, granted)
    assert len(ungranted) == 1
    assert ungranted[0].service_url == "github.com/x/b"


def test_ungranted_same_grant_different_service_not_satisfied(tmp_path: Path) -> None:
    """Identity is (service, grant): the same grant payload under a different
    service is still ungranted."""
    manifest = _manifest_with_consumes(tmp_path / "m", _consume_block("github.com/x/a", "a", '{ key = "K" }'))
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/other",
            grant={"key": "K"},
            scope="global",
            provider_app_id=None,
        )
    ]
    ungranted = manifest_ungranted_permissions_v2(manifest, granted)
    assert len(ungranted) == 1
    assert ungranted[0].service_url == "github.com/x/a"


def test_ungranted_app_scoped_grant_counts_as_held(tmp_path: Path) -> None:
    """A permission held under 'app' scope (not just 'global') still counts as
    already granted — the diff is scope-insensitive, so an update won't
    re-prompt for something the app already holds under any scope."""
    manifest = _manifest_with_consumes(tmp_path / "m", _consume_block("github.com/x/svc", "svc", '{ key = "K" }'))
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/svc",
            grant={"key": "K"},
            scope="app",
            provider_app_id="prov1",
        )
    ]
    assert manifest_ungranted_permissions_v2(manifest, granted) == []


# ─── the reload gate ──────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("perm-gate"), port=20900)
    init_db(c.db_path)
    return c


def _seed_perm_app(cfg: Any, repo_path: str) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, 'perm-app', '1.0', ?, 20901, 'running')",
            (app_id, repo_path),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def test_gate_returns_none_when_no_new_permissions(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, "")  # no consumes
    app_id = _seed_perm_app(cfg, str(repo))

    assert _gate_update_review(app_id, str(repo), approve_new_permissions=False) is None


def test_gate_refuses_new_permission_without_approval(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    app_id = _seed_perm_app(cfg, str(repo))

    result = _gate_update_review(app_id, str(repo), approve_new_permissions=False)
    assert result is not None
    assert result.ok is False
    assert len(result.permissions_required) == 1
    assert result.permissions_required[0]["service_url"] == "github.com/x/secrets"
    assert result.permissions_required[0]["shortname"] == "secrets"
    # Nothing was granted (the gate must not persist anything when refusing).
    assert get_all_permissions_v2(consumer_app_id=app_id) == []


def test_gate_grants_and_proceeds_when_approved(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    app_id = _seed_perm_app(cfg, str(repo))

    result = _gate_update_review(app_id, str(repo), approve_new_permissions=True)
    assert result is None
    granted = get_all_permissions_v2(consumer_app_id=app_id)
    assert len(granted) == 1
    assert granted[0].service_url == "github.com/x/secrets"
    assert granted[0].grant == {"key": "API_KEY"}


def test_gate_ignores_already_granted_permission(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    app_id = _seed_perm_app(cfg, str(repo))
    grant_permission_v2(consumer_app_id=app_id, service_url="github.com/x/secrets", grant_payload={"key": "API_KEY"})

    # Already held -> not "new" -> gate passes without prompting.
    assert _gate_update_review(app_id, str(repo), approve_new_permissions=False) is None


def test_gate_returns_none_for_unparseable_manifest(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openhost.toml").write_text("not = valid [ toml")
    app_id = _seed_perm_app(cfg, str(repo))

    # A broken manifest isn't the gate's problem; the reload path surfaces it.
    assert _gate_update_review(app_id, str(repo), approve_new_permissions=False) is None


# ─── the update delta: gate only author-added permissions ─────────────────────


def test_newly_declared_empty_when_previous_manifest_also_declared_it(tmp_path: Path) -> None:
    """Revoke survives update: a permission declared by BOTH the previous and the
    new manifest, but not granted (revoked), is NOT part of the delta."""
    consumes = _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    new_manifest = _manifest_with_consumes(tmp_path / "new", consumes)
    prev_raw = _CONSUMES.format(consumes=consumes)  # previous manifest declared the same perm
    assert manifest_newly_declared_permissions_v2(new_manifest, [], prev_raw) == []


def test_newly_declared_lists_author_added_permission(tmp_path: Path) -> None:
    """A permission the new manifest declares that the previous manifest did not is in the delta."""
    prev_consumes = _consume_block("github.com/x/a", "a", '{ key = "OLD" }')
    new_consumes = prev_consumes + _consume_block("github.com/x/b", "b", '{ key = "NEW" }')
    new_manifest = _manifest_with_consumes(tmp_path / "new", new_consumes)
    prev_raw = _CONSUMES.format(consumes=prev_consumes)
    new = manifest_newly_declared_permissions_v2(new_manifest, [], prev_raw)
    assert len(new) == 1
    assert new[0].service_url == "github.com/x/b"
    assert new[0].grant == {"key": "NEW"}


def test_newly_declared_excludes_already_granted(tmp_path: Path) -> None:
    """Even a newly-declared permission isn't re-listed if it's somehow already granted."""
    consumes = _consume_block("github.com/x/b", "b", '{ key = "NEW" }')
    new_manifest = _manifest_with_consumes(tmp_path / "new", consumes)
    prev_raw = _CONSUMES.format(consumes="")  # previous declared nothing
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/b",
            grant={"key": "NEW"},
            scope="global",
            provider_app_id=None,
        )
    ]
    assert manifest_newly_declared_permissions_v2(new_manifest, granted, prev_raw) == []


def test_newly_declared_falls_back_to_ungranted_without_previous_manifest(tmp_path: Path) -> None:
    """No previous manifest (None / legacy row): fall back to the full grant-state diff."""
    consumes = _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    m = _manifest_with_consumes(tmp_path / "m", consumes)
    fallback = manifest_newly_declared_permissions_v2(m, [], None)
    assert len(fallback) == 1
    assert fallback[0].service_url == "github.com/x/secrets"


def test_gate_does_not_reprompt_revoked_permission_on_update(cfg: Any, tmp_path: Path) -> None:
    """End-to-end gate: a revoked permission still declared in the (unchanged) manifest
    is not gated on update, because the previous manifest declared it too."""
    repo = tmp_path / "repo"
    consumes = _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    _manifest_with_consumes(repo, consumes)
    app_id = _seed_perm_app(cfg, str(repo))
    prev_raw = _CONSUMES.format(consumes=consumes)
    # No permissions_v2 row for it (owner revoked, or never granted).
    assert (
        _gate_update_review(app_id, str(repo), approve_new_permissions=False, previous_manifest_raw=prev_raw) is None
    )


def test_gate_prompts_for_author_added_permission_on_update(cfg: Any, tmp_path: Path) -> None:
    """End-to-end gate: a permission the update newly declares (not in the previous
    manifest) is still gated for approval."""
    repo = tmp_path / "repo"
    prev_consumes = _consume_block("github.com/x/a", "a", '{ key = "OLD" }')
    new_consumes = prev_consumes + _consume_block("github.com/x/b", "b", '{ key = "NEW" }')
    _manifest_with_consumes(repo, new_consumes)
    app_id = _seed_perm_app(cfg, str(repo))
    prev_raw = _CONSUMES.format(consumes=prev_consumes)
    result = _gate_update_review(app_id, str(repo), approve_new_permissions=False, previous_manifest_raw=prev_raw)
    assert result is not None
    assert result.ok is False
    assert [p["service_url"] for p in result.permissions_required] == ["github.com/x/b"]


# ─── the /reload_app route end-to-end ─────────────────────────────────────────


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_git_app_with_consumes(cfg: Any, repo: Path, consumes: str) -> str:
    """Seed a running app whose on-disk repo is a git checkout declaring
    ``consumes`` permissions, with a repo_url so ``update`` can run git_pull."""
    _manifest_with_consumes(repo, consumes)
    (repo / ".git").mkdir()  # marks it a git checkout
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status) "
            "VALUES (?, 'perm-app', '1.0', ?, 'https://github.com/x/perm-app', 20901, 'running')",
            (app_id, str(repo)),
        )
        db.commit()
    finally:
        db.close()
    return app_id


@contextlib.contextmanager
def _mocked_reload_side_effects() -> Iterator[dict[str, Any]]:
    """Stub out the heavy/side-effecting bits of an update reload so route tests
    exercise the permission gate hermetically: a successful git pull, no ref
    re-pin, no container stop, no background reload thread."""
    with (
        patch("compute_space.web.routes.api.apps.git_pull", return_value=(True, None)),
        patch("compute_space.web.routes.api.apps._pin_refless_to_landed_branch", return_value=None),
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        yield {"stop": stop, "thread": thread}


def _app_status(cfg: Any, app_id: str) -> str:
    db = sqlite3.connect(cfg.db_path)
    try:
        return str(db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0])
    finally:
        db.close()


def test_reload_route_refuses_when_new_permission_unapproved(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(
        cfg, repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )

    with _mocked_reload_side_effects() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["permissions_required"][0]["service_url"] == "github.com/x/secrets"
    # The running app was NOT touched and no reload thread was spawned.
    m["stop"].assert_not_called()
    m["thread"].assert_not_called()
    assert get_all_permissions_v2(consumer_app_id=app_id) == []
    # App row stays 'running' (not flipped to 'building').
    assert _app_status(cfg, app_id) == "running"


def test_reload_route_grants_and_reloads_when_approved(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(
        cfg, repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )

    with _mocked_reload_side_effects() as m:
        resp = client.post(
            f"/reload_app/{app_id}",
            json={"update": True, "approve_new_permissions": True},
            cookies=cookies,
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # The permission was granted and the reload proceeded.
    granted = get_all_permissions_v2(consumer_app_id=app_id)
    assert len(granted) == 1
    assert granted[0].service_url == "github.com/x/secrets"
    m["stop"].assert_called_once()
    m["thread"].assert_called_once()


def test_reload_route_proceeds_when_no_new_permissions(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(cfg, repo, "")  # no consumes

    with _mocked_reload_side_effects() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    m["stop"].assert_called_once()
    m["thread"].assert_called_once()


def test_plain_reload_does_not_gate_declared_but_ungranted_permission(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    """A plain reload (update=False) deploys the manifest already on disk, so it
    must NOT re-prompt for a permission the owner declined at install and chose
    to keep running without. Only an actual code pull (update) can gate."""
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(
        cfg, repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )

    with (
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        resp = client.post(f"/reload_app/{app_id}", json={"update": False}, cookies=cookies)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Reload proceeded; nothing was granted (plain reload never grants).
    stop.assert_called_once()
    thread.assert_called_once()
    assert get_all_permissions_v2(consumer_app_id=app_id) == []


def test_reload_route_lists_all_new_permissions(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    """An update declaring several new permissions reports all of them."""
    repo = tmp_path / "repo"
    consumes = _consume_block("github.com/x/a", "a", '{ key = "K1" }') + _consume_block(
        "github.com/x/b", "b", '"FULL"'
    )
    app_id = _seed_git_app_with_consumes(cfg, repo, consumes)

    with _mocked_reload_side_effects():
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)

    body = resp.json()
    assert body["ok"] is False
    services = sorted(p["service_url"] for p in body["permissions_required"])
    assert services == ["github.com/x/a", "github.com/x/b"]
    assert get_all_permissions_v2(consumer_app_id=app_id) == []


def test_reload_route_only_gates_the_newly_added_permission(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    """If the app already holds one declared permission, an update adding a
    second is gated only on the new one; approving grants only the new one
    (the already-held one is untouched, INSERT OR IGNORE)."""
    repo = tmp_path / "repo"
    consumes = _consume_block("github.com/x/a", "a", '{ key = "OLD" }') + _consume_block(
        "github.com/x/b", "b", '{ key = "NEW" }'
    )
    app_id = _seed_git_app_with_consumes(cfg, repo, consumes)
    grant_permission_v2(consumer_app_id=app_id, service_url="github.com/x/a", grant_payload={"key": "OLD"})

    # Without approval: refused, and only the NEW one is listed.
    with _mocked_reload_side_effects() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)
    body = resp.json()
    assert body["ok"] is False
    assert len(body["permissions_required"]) == 1
    assert body["permissions_required"][0]["service_url"] == "github.com/x/b"
    m["thread"].assert_not_called()

    # With approval: proceeds; now both are held.
    with _mocked_reload_side_effects() as m:
        resp = client.post(
            f"/reload_app/{app_id}", json={"update": True, "approve_new_permissions": True}, cookies=cookies
        )
    assert resp.json() == {"ok": True}
    held = {
        (p.service_url, tuple(sorted(p.grant.items())) if isinstance(p.grant, dict) else p.grant)
        for p in get_all_permissions_v2(consumer_app_id=app_id)
    }
    assert ("github.com/x/a", (("key", "OLD"),)) in held
    assert ("github.com/x/b", (("key", "NEW"),)) in held
    m["thread"].assert_called_once()


# ─── regression: a refused update must not leave the pulled code on disk ───────
# Guards against the bug where the git pull advanced the working tree to the
# new (unapproved) version, the gate refused, but the tree stayed on the new
# version — so a later PLAIN reload (which is intentionally not gated, on the
# assumption the on-disk manifest matches the running one) would silently deploy
# the unapproved code + its new permissions.


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_two_commit_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Create an 'origin' repo with v1 (no consumes) then v2 (adds a consume),
    and a clone checked out at v1. Returns (clone, origin, v1_sha)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "-C", str(origin), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "user.name", "t"], check=True)
    (origin / "openhost.toml").write_text(_CONSUMES.format(consumes=""))
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "v1 no consumes")
    v1 = _git(origin, "rev-parse", "HEAD")
    # v2 adds a new consume
    (origin / "openhost.toml").write_text(
        _CONSUMES.format(consumes=_consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    )
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "v2 adds consume")

    v2 = _git(origin, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    # The app is "running" v1: detach the working tree at v1. The (mocked) update
    # pull will fast-forward it to v2, which the gate must then roll back.
    _git(clone, "checkout", "-q", v1)
    return clone, origin, v1, v2


def _seed_running_app_at(cfg: Any, repo: Path, origin: Path) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status) "
            "VALUES (?, 'perm-app', '1.0', ?, ?, 20955, 'running')",
            (app_id, str(repo), str(origin)),  # repo_url = origin so git_pull fetches v2
        )
        db.commit()
    finally:
        db.close()
    return app_id


def test_refused_update_rolls_back_working_tree(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    clone, _origin, v1, v2 = _init_two_commit_repo(tmp_path)
    app_id = _seed_running_app_at(cfg, clone, _origin)

    # Simulate the update pull advancing the working tree to v2 (which declares a
    # new, unapproved permission). Our gate must then refuse AND roll the tree
    # back to v1, so a later plain reload can't deploy the unapproved version.
    def _fake_pull(*_a: Any, **_k: Any) -> tuple[bool, None]:
        _git(clone, "reset", "--hard", v2)
        return True, None

    with (
        patch("compute_space.web.routes.api.apps.git_pull", side_effect=_fake_pull),
        patch("compute_space.web.routes.api.apps._pin_refless_to_landed_branch", return_value=None),
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        # sanity: the pulled v2 really does declare a new consume
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)

    assert resp.status_code == 200
    assert resp.json()["ok"] is False, "update declaring a new permission must be refused"
    stop.assert_not_called()
    thread.assert_not_called()
    assert get_all_permissions_v2(consumer_app_id=app_id) == []
    # The working tree must be rolled back to v1 — NOT left on the pulled v2 — so
    # a subsequent plain (ungated) reload can't deploy the unapproved version.
    assert _git(clone, "rev-parse", "HEAD") == v1
    assert "consumes" not in (clone / "openhost.toml").read_text()


# ─── the settings diff: gate on ANY changed manifest setting ──────────────────

_BASE = """\
[app]
name = "perm-app"
version = "{version}"

[runtime.container]
image = "Dockerfile"
port = 5000

[resources]
memory_mb = {memory}
"""


def test_settings_changes_empty_without_previous() -> None:
    m = parse_manifest_from_string(_BASE.format(version="1.0.0", memory=128))
    assert manifest_settings_changes(m, None) == []


def test_settings_changes_empty_when_identical() -> None:
    raw = _BASE.format(version="1.0.0", memory=128)
    assert manifest_settings_changes(parse_manifest_from_string(raw), raw) == []


def test_settings_changes_unparseable_previous_is_empty() -> None:
    m = parse_manifest_from_string(_BASE.format(version="1.0.0", memory=128))
    assert manifest_settings_changes(m, "not = valid [ toml") == []


def test_settings_changes_detects_version_and_memory() -> None:
    prev = _BASE.format(version="1.0.0", memory=128)
    new = parse_manifest_from_string(_BASE.format(version="2.0.0", memory=256))
    changes = {c.label: (c.old, c.new) for c in manifest_settings_changes(new, prev)}
    assert changes["Version"] == ("1.0.0", "2.0.0")
    assert changes["Memory (MB)"] == ("128", "256")


def test_gate_refuses_settings_change_without_approval(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openhost.toml").write_text(_BASE.format(version="2.0.0", memory=256))
    app_id = _seed_perm_app(cfg, str(repo))
    prev_raw = _BASE.format(version="1.0.0", memory=128)

    result = _gate_update_review(app_id, str(repo), approve_new_permissions=False, previous_manifest_raw=prev_raw)
    assert result is not None
    assert result.ok is False
    assert result.review_required is True
    assert result.permissions_required == []
    labels = {c["label"] for c in result.settings_changed}
    assert {"Version", "Memory (MB)"} <= labels


def test_gate_proceeds_on_settings_change_when_approved(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openhost.toml").write_text(_BASE.format(version="2.0.0", memory=256))
    app_id = _seed_perm_app(cfg, str(repo))
    prev_raw = _BASE.format(version="1.0.0", memory=128)

    # No permissions involved, so approval simply lets the update proceed.
    assert _gate_update_review(app_id, str(repo), approve_new_permissions=True, previous_manifest_raw=prev_raw) is None


def test_gate_passes_when_manifest_unchanged(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = _BASE.format(version="1.0.0", memory=128)
    (repo / "openhost.toml").write_text(raw)
    app_id = _seed_perm_app(cfg, str(repo))

    assert _gate_update_review(app_id, str(repo), approve_new_permissions=False, previous_manifest_raw=raw) is None


def _seed_git_app_with_manifest_raw(cfg: Any, repo: Path, on_disk_toml: str, manifest_raw: str) -> str:
    """Seed a running git-backed app whose on-disk manifest differs from the
    stored (running) manifest_raw, so an update gates on the settings diff."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "openhost.toml").write_text(on_disk_toml)
    (repo / ".git").mkdir()
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, repo_url, manifest_raw, local_port, status) "
            "VALUES (?, 'perm-app', '1.0', ?, 'https://github.com/x/perm-app', ?, 20901, 'running')",
            (app_id, str(repo), manifest_raw),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def test_reload_route_gates_on_settings_change(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_manifest_raw(
        cfg, repo, _BASE.format(version="2.0.0", memory=256), _BASE.format(version="1.0.0", memory=128)
    )

    with _mocked_reload_side_effects() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)
    body = resp.json()
    assert body["ok"] is False
    assert body["review_required"] is True
    assert {c["label"] for c in body["settings_changed"]} >= {"Version", "Memory (MB)"}
    m["thread"].assert_not_called()

    with _mocked_reload_side_effects() as m:
        resp = client.post(
            f"/reload_app/{app_id}", json={"update": True, "approve_new_permissions": True}, cookies=cookies
        )
    assert resp.json() == {"ok": True}
    m["thread"].assert_called_once()
