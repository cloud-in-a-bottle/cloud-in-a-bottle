from __future__ import annotations

from pathlib import Path

import pytest
from litestar import Request

from compute_space.core.domains import Domain
from compute_space.tests._litestar_helpers import make_http_scope
from compute_space.tests.conftest import _make_test_config
from compute_space.web.auth.auth import auth_required_response
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY


@pytest.mark.parametrize(
    "method,expected", [("GET", 302), ("HEAD", 302), ("POST", 401), ("PUT", 401), ("PATCH", 401), ("DELETE", 401)]
)
def test_only_get_and_head_redirect_to_login(tmp_path: Path, method: str, expected: int) -> None:
    # a followed 302 is re-issued as a bodyless GET, so unsafe methods get a 401 instead.
    _make_test_config(tmp_path, zone_domain="testzone.local", tls_enabled=True)
    scope = make_http_scope(
        method,
        "/feeds/refresh",
        host="miniflux.testzone.local",
        extra_scope={ZONE_SCOPE_KEY: Domain(name="testzone.local", tls=True)},
    )
    response = auth_required_response(Request(scope))  # type: ignore[arg-type]
    assert response.status_code == expected
