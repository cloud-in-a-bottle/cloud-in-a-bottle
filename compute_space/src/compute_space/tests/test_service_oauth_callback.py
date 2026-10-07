import json
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from litestar.testing import TestClient

from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.tests.test_cors_preflight import _make_app
from compute_space.tests.test_multidomain_proxy_integration import _RecordingBackend
from compute_space.tests.test_multidomain_proxy_integration import _seed_app
from compute_space.tests.test_multidomain_proxy_integration import backend as backend

CALLBACK_URL = "/api/services/v2/oauth_callback"
APP_NAME = "oauth-provider"
PARAMS = {"state": json.dumps({"app": APP_NAME, "nonce": "test-nonce"}), "code": "test-code"}


@pytest.mark.parametrize("status", ["running", "stopped", "starting"])
@pytest.mark.parametrize("public_paths", [[], ["/unrelated"], ["/callbacks"], ["/callback/child"]])
def test_private_callback_matches_missing_without_backend_access(
    tmp_path: Path, backend: _RecordingBackend, status: str, public_paths: list[str]
) -> None:
    cfg = _make_test_config(tmp_path)
    with TestClient(app=_make_app()) as client:
        missing = client.get(CALLBACK_URL, params=PARAMS)
        _seed_app(cfg.db_path, APP_NAME, backend.server_port, public_paths)
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
    assert private.headers == missing.headers
    assert backend.requests == []


@pytest.mark.parametrize("public_paths", [["/callback"], ["/"], ["/callback", "/device", "/grant"]])
@pytest.mark.parametrize("status", ["running", "stopped"])
def test_public_callback_preserves_forwarding_and_status_check(
    tmp_path: Path, backend: _RecordingBackend, public_paths: list[str], status: str
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_app(cfg.db_path, APP_NAME, backend.server_port, public_paths)
    with closing(open_db(cfg)) as db:
        db.execute("UPDATE apps SET status = ? WHERE name = ?", (status, APP_NAME))
        db.commit()
    with TestClient(app=_make_app()) as client:
        response = client.get(CALLBACK_URL, params=PARAMS)

    if status == "stopped":
        assert response.status_code == 503
        assert response.json() == {
            "status_code": 503,
            "detail": f"App '{APP_NAME}' is not running",
            "extra": {"code": "service_not_available"},
        }
        assert backend.requests == []
    else:
        assert response.status_code == 200
        assert response.content == b"backend-ok"
        assert len(backend.requests) == 1
        method, target, body = backend.requests[0]
        assert method == "GET"
        assert body == b""
        assert urlsplit(target).path == "/callback"
        assert parse_qs(urlsplit(target).query) == {key: [value] for key, value in PARAMS.items()}
