import json
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from litestar.testing import TestClient

from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.tests.test_multidomain_proxy_integration import _seed_app
from compute_space.web.routes.redirect import redirect_routes

CALLBACK_URL = "/redirect/oauth_callback"
APP_NAME = "oauth-provider"
PARAMS = {"state": json.dumps({"app": APP_NAME, "nonce": "test-nonce"}), "code": "test-code"}


def _client() -> TestClient:  # type: ignore[type-arg]
    return TestClient(app=make_test_app(redirect_routes), base_url="http://testzone.local:8080")


@pytest.mark.parametrize("status", ["running", "stopped", "starting"])
@pytest.mark.parametrize("public_paths", [[], ["/unrelated"], ["/callbacks"], ["/callback/child"]])
def test_private_callback_matches_missing(tmp_path: Path, status: str, public_paths: list[str]) -> None:
    cfg = _make_test_config(tmp_path)
    with _client() as client:
        missing = client.get(CALLBACK_URL, params=PARAMS)
        _seed_app(cfg.db_path, APP_NAME, 19700, public_paths)
        with closing(open_db(cfg)) as db:
            db.execute("UPDATE apps SET status = ? WHERE name = ?", (status, APP_NAME))
            db.commit()
        private = client.get(CALLBACK_URL, params=PARAMS)

    assert missing.status_code == private.status_code == 503
    assert private.json() == {
        "status_code": 503,
        "detail": f"App '{APP_NAME}' not found",
        "extra": {"code": "service_not_available"},
    }
    assert private.content == missing.content


@pytest.mark.parametrize("public_paths", [["/callback"], ["/"], ["/callback", "/device", "/grant"]])
def test_public_callback_redirects_to_the_app_with_the_full_query(tmp_path: Path, public_paths: list[str]) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_app(cfg.db_path, APP_NAME, 19700, public_paths)
    with _client() as client:
        response = client.get(CALLBACK_URL, params={**PARAMS, "scope": "a b"}, follow_redirects=False)

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    # The app's subdomain, on the domain and access port the request arrived on.
    assert (location.scheme, location.netloc, location.path) == (
        "http",
        f"{APP_NAME}.testzone.local:8080",
        "/callback",
    )
    assert parse_qs(location.query) == {**{key: [value] for key, value in PARAMS.items()}, "scope": ["a b"]}


def test_stopped_app_is_reported(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_app(cfg.db_path, APP_NAME, 19700, ["/callback"])
    with closing(open_db(cfg)) as db:
        db.execute("UPDATE apps SET status = 'stopped' WHERE name = ?", (APP_NAME,))
        db.commit()
    with _client() as client:
        response = client.get(CALLBACK_URL, params=PARAMS, follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["detail"] == f"App '{APP_NAME}' is not running"


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"code": "x"}, "Missing state parameter"),
        ({"state": "not json"}, "Invalid state parameter"),
        ({"state": json.dumps({"nonce": "x"})}, "Missing app in state"),
        ({"state": json.dumps(["oauth-provider"])}, "Missing app in state"),
    ],
)
def test_bad_state_is_rejected(tmp_path: Path, params: dict[str, str], detail: str) -> None:
    _make_test_config(tmp_path)
    with _client() as client:
        response = client.get(CALLBACK_URL, params=params, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"] == detail
