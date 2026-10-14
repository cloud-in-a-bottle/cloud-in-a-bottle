import json
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from litestar.testing import TestClient

from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_multidomain_proxy_integration import _seed_app
from compute_space.web.routes.redirect import redirect_routes

CALLBACK_URL = "/redirect/oauth_callback"
APP_NAME = "oauth-provider"
PARAMS = {"state": json.dumps({"app": APP_NAME, "nonce": "test-nonce"}), "code": "test-code"}


def _client() -> TestClient:  # type: ignore[type-arg]
    return TestClient(app=make_test_app(redirect_routes), base_url="http://testzone.local:8080")


@pytest.mark.parametrize("public_paths", [None, [], ["/unrelated"], ["/callback"]])
def test_redirects_without_revealing_whether_the_app_exists(tmp_path: Path, public_paths: list[str] | None) -> None:
    cfg = _make_test_config(tmp_path)
    if public_paths is not None:
        _seed_app(cfg.db_path, APP_NAME, 19700, public_paths)
    with _client() as client:
        response = client.get(CALLBACK_URL, params={**PARAMS, "scope": "a b"}, follow_redirects=False)

    # The same answer whether the app is missing, private or public: the app's host applies the usual proxy rules.
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    # The app's subdomain, on the domain and access port the request arrived on.
    assert (location.scheme, location.netloc, location.path) == (
        "http",
        f"{APP_NAME}.testzone.local:8080",
        "/callback",
    )
    assert parse_qs(location.query) == {**{key: [value] for key, value in PARAMS.items()}, "scope": ["a b"]}


def test_missing_state_is_rejected(tmp_path: Path) -> None:
    _make_test_config(tmp_path)
    with _client() as client:
        response = client.get(CALLBACK_URL, params={"code": "x"}, follow_redirects=False)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "state",
    [
        "not json",
        json.dumps({"nonce": "x"}),
        json.dumps(["oauth-provider"]),
        json.dumps({"app": 5}),
        json.dumps({"app": "../evil"}),
        json.dumps({"app": "Bad_Name"}),
    ],
)
def test_bad_state_is_rejected(tmp_path: Path, state: str) -> None:
    _make_test_config(tmp_path)
    with _client() as client:
        response = client.get(CALLBACK_URL, params={"state": state, "code": "x"}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid state parameter"
