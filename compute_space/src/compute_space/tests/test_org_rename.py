"""The org-rename reconcile is retired; this asserts it stays inert.

The rewrite was removed rather than fixed, because the threat it addressed is
closed (we hold the old organization name, so its redirects cannot be hijacked)
and because it only ever ran on instances whose owners actively update. See
``core/org_rename`` for the full reasoning.

These tests exist so that nobody reintroduces the rewrite by accident. The
deleted version had a correctness bug worth remembering: it moved only the owner
segment, so a stored ``imbue-openhost/openhost-nextcloud`` would have become
``cloud-in-a-bottle/openhost-nextcloud``, which does not exist. The old-owner path
resolves; that one does not.
"""

import sqlite3

import pytest

from compute_space.core.org_rename import reconcile_app_repo_urls


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE apps (app_id TEXT PRIMARY KEY, name TEXT, repo_url TEXT, repo_path TEXT NOT NULL DEFAULT '')"
    )
    return conn


def _insert(conn: sqlite3.Connection, app_id: str, name: str, url: str) -> None:
    conn.execute("INSERT INTO apps (app_id, name, repo_url) VALUES (?, ?, ?)", (app_id, name, url))
    conn.commit()


def _url(conn: sqlite3.Connection, name: str) -> str:
    return str(conn.execute("SELECT repo_url FROM apps WHERE name = ?", (name,)).fetchone()[0])


def test_leaves_a_pre_rename_owner_alone(db: sqlite3.Connection) -> None:
    """A URL under the old owner still resolves via redirect, so it must not move."""
    url = "https://github.com/imbue-openhost/bottled-navidrome@master"
    _insert(db, "a1", "navidrome", url)
    assert reconcile_app_repo_urls(db) == 0
    assert _url(db, "navidrome") == url


def test_leaves_a_pre_rename_repo_name_alone(db: sqlite3.Connection) -> None:
    """The case the deleted rewrite got wrong.

    imbue-openhost/openhost-nextcloud resolves (old owner, old repo name).
    Rewriting only the owner would have produced
    cloud-in-a-bottle/openhost-nextcloud, which does not exist.
    """
    url = "https://github.com/imbue-openhost/openhost-nextcloud"
    _insert(db, "a2", "nextcloud", url)
    assert reconcile_app_repo_urls(db) == 0
    assert _url(db, "nextcloud") == url


def test_leaves_current_urls_alone(db: sqlite3.Connection) -> None:
    url = "https://github.com/cloud-in-a-bottle/bottled-filestash"
    _insert(db, "a3", "filestash", url)
    assert reconcile_app_repo_urls(db) == 0
    assert _url(db, "filestash") == url


def test_never_raises_on_a_broken_db() -> None:
    """It runs during boot, so it must not be able to fail one."""
    conn = sqlite3.connect(":memory:")  # no apps table at all
    conn.row_factory = sqlite3.Row
    assert reconcile_app_repo_urls(conn) == 0
