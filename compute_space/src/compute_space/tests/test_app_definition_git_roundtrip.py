import asyncio
import json
import sqlite3
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from compute_space.core.app_definition_loader import definition_plan
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_definitions import export_app_definitions
from compute_space.core.git_ops import clone_repo
from compute_space.core.git_ops import parse_repo_url
from compute_space.tests.test_app_definitions import seed_app
from compute_space.tests.test_clone_ref import _git
from compute_space.tests.test_clone_ref import _two_commit_origin
from compute_space.web.helpers.app_definition_export import dump_export_yaml


@pytest.mark.parametrize(
    ("repo_name", "ref"),
    [
        ("Project Name.git", "andrew/stable"),
        ("app.git", "andrew/release%candidate"),
        ("app.git", "andrew/main%3fsecret"),
    ],
)
def test_exported_git_sources_reach_installer_unchanged(
    tmp_path: Path, db: sqlite3.Connection, repo_name: str, ref: str
) -> None:
    origin, selected_commit, _ = _two_commit_origin(tmp_path)
    _git(origin, "branch", ref, selected_commit)
    remote_dir = tmp_path / "remotes"
    remote_dir.mkdir()
    remote = remote_dir / repo_name
    _git(tmp_path, "clone", "--bare", str(origin), str(remote))
    _git(remote, "update-server-info")
    handler = partial(SimpleHTTPRequestHandler, directory=str(remote_dir))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/{quote(repo_name)}@{ref}"
            seed_app(db, "demo", repo_url=url)
            exported = asyncio.run(export_app_definitions(db, str(tmp_path), "sharing"))
            document = parse_definition(dump_export_yaml(json.loads(exported)))
            # Plan against an empty destination, then use the installer's real Git clone operation.
            db.execute("DELETE FROM apps WHERE name = 'demo'")
            db.commit()
            install = definition_plan(document, db, str(tmp_path)).apps[0].install
            assert install is not None and install.repo_url == url
            base_url, selected_ref = parse_repo_url(install.repo_url)
            clone = tmp_path / "imported"
            asyncio.run(clone_repo(str(clone), base_url, selected_ref, None))
            assert _git(clone, "rev-parse", "HEAD") == selected_commit
            assert _git(clone, "symbolic-ref", "--short", "HEAD") == ref
        finally:
            server.shutdown()
            thread.join()
