"""Tests for moving persisted app repo URLs off a renamed GitHub org.

The rewrite runs against URLs that instances have already stored, so the
dangerous failure is over-matching: rewriting a URL that points somewhere else
would silently redirect an app's source to a repository we do not control.
Most of these cases are therefore about what must *not* change.
"""

import sqlite3
from pathlib import Path
from unittest import mock

import git
import pytest

from compute_space.core import org_rename
from compute_space.core.org_rename import OLD_ORG
from compute_space.core.org_rename import reconcile_app_repo_urls
from compute_space.core.org_rename import rewrite_owner

NEW = "cloud-in-a-bottle"


def _rw(url: str, new_org: str = NEW) -> str | None:
    return rewrite_owner(url, old_org=OLD_ORG, new_org=new_org)


# --- the sequencing guard ------------------------------------------------


def test_disabled_until_new_org_is_set() -> None:
    """Shipping with NEW_ORG empty must be a total no-op.

    Rewriting before the org is actually renamed would point instances at an
    owner that does not exist, which is worse than the redirect dependency.
    """
    assert org_rename.NEW_ORG == "", "NEW_ORG must ship empty; set it only with the rename"
    assert _rw(f"https://github.com/{OLD_ORG}/bottled-navidrome", new_org="") is None


def test_no_op_when_new_equals_old() -> None:
    assert _rw(f"https://github.com/{OLD_ORG}/x", new_org=OLD_ORG) is None


# --- what must be rewritten ---------------------------------------------


def test_rewrites_the_owner_segment() -> None:
    assert _rw(f"https://github.com/{OLD_ORG}/bottled-navidrome") == f"https://github.com/{NEW}/bottled-navidrome"


def test_preserves_ref_suffix() -> None:
    """apps.repo_url stores a pip-style @ref; losing it would change the
    installed revision."""
    assert (
        _rw(f"https://github.com/{OLD_ORG}/bottled-navidrome@master")
        == f"https://github.com/{NEW}/bottled-navidrome@master"
    )


def test_preserves_git_suffix_and_subpaths() -> None:
    assert _rw(f"https://github.com/{OLD_ORG}/openhost.git") == f"https://github.com/{NEW}/openhost.git"
    assert _rw(f"https://github.com/{OLD_ORG}/openhost/tree/main") == f"https://github.com/{NEW}/openhost/tree/main"


def test_owner_match_is_case_insensitive() -> None:
    """GitHub owner names are case-insensitive, so a stored Imbue-OpenHost must
    still be migrated."""
    assert _rw("https://github.com/Imbue-OpenHost/bottled-lila") == f"https://github.com/{NEW}/bottled-lila"


def test_scheme_less_url_keeps_its_shape() -> None:
    assert _rw(f"github.com/{OLD_ORG}/bottled-lila") == f"github.com/{NEW}/bottled-lila"


# --- what must NOT be rewritten -----------------------------------------


def test_leaves_other_owners_alone() -> None:
    assert _rw("https://github.com/imbue-ai/sculptor") is None
    assert _rw("https://github.com/CarlKho-Minerva/openhost-cap") is None


def test_leaves_look_alike_hosts_alone() -> None:
    """github.com.evil.example is not GitHub; rewriting it would hand an
    attacker-controlled host our new owner name."""
    assert _rw(f"https://github.com.evil.example/{OLD_ORG}/x") is None
    assert _rw(f"https://notgithub.com/{OLD_ORG}/x") is None


def test_leaves_other_forges_alone() -> None:
    assert _rw(f"https://gitlab.com/{OLD_ORG}/x") is None


def test_leaves_ssh_urls_alone() -> None:
    assert _rw(f"git@github.com:{OLD_ORG}/x.git") is None
    assert _rw(f"ssh://git@github.com/{OLD_ORG}/x.git") is None


def test_does_not_touch_a_repo_named_after_the_owner() -> None:
    """Only the owner segment moves. A repo whose *name* contains the old org
    keeps its name."""
    assert _rw(f"https://github.com/{OLD_ORG}/{OLD_ORG}-tools") == f"https://github.com/{NEW}/{OLD_ORG}-tools"
    assert _rw(f"https://github.com/someone/{OLD_ORG}") is None


def test_leaves_local_and_file_urls_alone() -> None:
    assert _rw("file:///home/host/openhost/apps/oauth_provider") is None
    assert _rw("") is None


# --- the DB reconcile ----------------------------------------------------


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE apps (app_id TEXT PRIMARY KEY, name TEXT, repo_url TEXT, repo_path TEXT NOT NULL DEFAULT '')"
    )
    return conn


def _insert(conn: sqlite3.Connection, app_id: str, name: str, url: str | None, path: str = "") -> None:
    conn.execute("INSERT INTO apps (app_id, name, repo_url, repo_path) VALUES (?, ?, ?, ?)", (app_id, name, url, path))
    conn.commit()


def _urls(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {r["name"]: r["repo_url"] for r in conn.execute("SELECT name, repo_url FROM apps")}


def test_reconcile_is_a_no_op_while_disabled(db: sqlite3.Connection) -> None:
    _insert(db, "a1", "navidrome", f"https://github.com/{OLD_ORG}/bottled-navidrome@master")
    assert reconcile_app_repo_urls(db) == 0
    assert _urls(db)["navidrome"] == f"https://github.com/{OLD_ORG}/bottled-navidrome@master"


def test_reconcile_rewrites_only_matching_rows(db: sqlite3.Connection) -> None:
    _insert(db, "a1", "navidrome", f"https://github.com/{OLD_ORG}/bottled-navidrome@master")
    _insert(db, "a2", "sculptor", "https://github.com/imbue-ai/sculptor")
    _insert(db, "a3", "oauth-provider", "file:///home/host/openhost/apps/oauth_provider")
    _insert(db, "a4", "nourl", None)

    with mock.patch.object(org_rename, "NEW_ORG", NEW):
        assert reconcile_app_repo_urls(db) == 1

    urls = _urls(db)
    assert urls["navidrome"] == f"https://github.com/{NEW}/bottled-navidrome@master"
    assert urls["sculptor"] == "https://github.com/imbue-ai/sculptor"
    assert urls["oauth-provider"] == "file:///home/host/openhost/apps/oauth_provider"
    assert urls["nourl"] is None


def test_reconcile_is_idempotent(db: sqlite3.Connection) -> None:
    _insert(db, "a1", "navidrome", f"https://github.com/{OLD_ORG}/bottled-navidrome")
    with mock.patch.object(org_rename, "NEW_ORG", NEW):
        assert reconcile_app_repo_urls(db) == 1
        assert reconcile_app_repo_urls(db) == 0
    assert _urls(db)["navidrome"] == f"https://github.com/{NEW}/bottled-navidrome"


def test_reconcile_updates_the_checkout_origin(db: sqlite3.Connection, tmp_path: Path) -> None:
    """The checkout's origin is what an update actually fetches from, so it has
    to move with the DB row."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    repo = git.Repo.init(checkout)
    repo.create_remote("origin", f"https://github.com/{OLD_ORG}/bottled-navidrome")

    _insert(db, "a1", "navidrome", f"https://github.com/{OLD_ORG}/bottled-navidrome", str(checkout))
    with mock.patch.object(org_rename, "NEW_ORG", NEW):
        assert reconcile_app_repo_urls(db) == 1

    assert repo.remotes.origin.url == f"https://github.com/{NEW}/bottled-navidrome"


def test_reconcile_survives_a_missing_checkout(db: sqlite3.Connection, tmp_path: Path) -> None:
    """A broken checkout must not stop the DB rewrite, and must not raise: this
    runs during boot."""
    _insert(db, "a1", "navidrome", f"https://github.com/{OLD_ORG}/bottled-navidrome", str(tmp_path / "gone"))
    with mock.patch.object(org_rename, "NEW_ORG", NEW):
        assert reconcile_app_repo_urls(db) == 1
    assert _urls(db)["navidrome"] == f"https://github.com/{NEW}/bottled-navidrome"


def test_reconcile_never_raises_on_a_broken_db() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row  # no apps table at all
    with mock.patch.object(org_rename, "NEW_ORG", NEW):
        assert reconcile_app_repo_urls(conn) == 0
