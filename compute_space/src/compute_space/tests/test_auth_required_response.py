"""Tests for auth_required_response's method-aware behaviour.

The load-bearing property: an unauthenticated *unsafe*-method request (POST/PUT/PATCH/DELETE) to a
protected path must NOT be answered with a 302→/login.  A browser that follows such a redirect re-issues
the request as a bodyless GET, which the target app rejects with 405 — silently downgrading the method and
dropping the body.  Only navigational GET/HEAD requests get the login redirect; everything else gets 403.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from litestar import Request

from compute_space.core.domains import Domain
from compute_space.tests._litestar_helpers import make_http_scope
from compute_space.tests.conftest import _make_test_config
from compute_space.web.auth.auth import auth_required_response
from compute_space.web.auth.auth import is_login_redirectable_method
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY


@pytest.fixture(autouse=True)
def _cfg(tmp_path: Path) -> Any:
    # auth_required_response -> login_required_redirect -> build_login_url reads the active config.
    return _make_test_config(tmp_path, zone_domain="testzone.local", tls_enabled=True)


def _request(method: str) -> Request[Any, Any, Any]:
    # SubdomainProxyMiddleware normally stashes the arriving Domain in ZONE_SCOPE_KEY; login_required_redirect
    # reads it via zone_for_request, so provide it directly in this middleware-less unit test.
    scope = make_http_scope(
        method,
        "/feeds/refresh",
        host="miniflux.testzone.local",
        extra_scope={ZONE_SCOPE_KEY: Domain(name="testzone.local", tls=True)},
    )
    return Request(scope)  # type: ignore[arg-type]


def test_get_redirects_to_login() -> None:
    response = auth_required_response(_request("GET"))
    assert response.status_code == 302
    # Redirect exposes its target on .url (the Location header is materialised only at render time).
    assert "/login?next=" in getattr(response, "url", "")


def test_head_redirects_to_login() -> None:
    # HEAD is a safe navigational method: a followed 302 stays lossless.
    assert auth_required_response(_request("HEAD")).status_code == 302


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unsafe_methods_get_403_not_redirect(method: str) -> None:
    response = auth_required_response(_request(method))
    assert response.status_code == 403
    # crucially, not a redirect: a Redirect would carry a target URL a browser follows (and downgrades to GET).
    assert not hasattr(response, "url")


def test_is_login_redirectable_method() -> None:
    assert is_login_redirectable_method("GET")
    assert is_login_redirectable_method("get")
    assert is_login_redirectable_method("HEAD")
    assert not is_login_redirectable_method("POST")
    assert not is_login_redirectable_method("delete")
