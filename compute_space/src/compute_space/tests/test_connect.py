"""The instance-side "Connect to Imbue" helpers (``core/connect.py``).

``build_connect_url`` composes the Imbue authorization URL (zone + instance
callback); ``exchange_code_for_credential`` swaps the one-time code for the shared
credential, raising ``ConnectError`` on any transport or protocol failure.  These
tests pin URL composition and every failure mode of the exchange (mocking
``httpx.post``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

import httpx
import pytest

from compute_space.core.connect import CONNECT_CALLBACK_PATH
from compute_space.core.connect import ConnectError
from compute_space.core.connect import build_connect_url
from compute_space.core.connect import exchange_code_for_credential
from compute_space.core.tls.keycloak import KeycloakClientCredentials

_IMBUE = "https://openhost.imbue.com"

# --- constant ----------------------------------------------------------------


def test_callback_path_constant() -> None:
    assert CONNECT_CALLBACK_PATH == "/api/settings/connect-imbue/callback"


# --- build_connect_url -------------------------------------------------------


def test_build_connect_url_basic_shape() -> None:
    url = build_connect_url(_IMBUE, "alice.selfhost.imbue.com", "https://alice.selfhost.imbue.com")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "openhost.imbue.com"
    assert parsed.path == "/connect/imbue"
    qs = parse_qs(parsed.query)
    assert qs["zone"] == ["alice.selfhost.imbue.com"]
    assert qs["callback"] == ["https://alice.selfhost.imbue.com/api/settings/connect-imbue/callback"]


def test_build_connect_url_callback_is_urlencoded_in_raw_string() -> None:
    url = build_connect_url(_IMBUE, "z.example.com", "https://z.example.com")
    # The callback's slashes/colons must be percent-encoded in the raw query.
    assert "callback=https%3A%2F%2Fz.example.com%2Fapi%2Fsettings%2Fconnect-imbue%2Fcallback" in url


def test_build_connect_url_strips_trailing_slash_on_frontend() -> None:
    url = build_connect_url(_IMBUE + "/", "z.example.com", "https://z.example.com")
    assert url.startswith(f"{_IMBUE}/connect/imbue?")
    assert "openhost.imbue.com//connect" not in url


def test_build_connect_url_strips_trailing_slash_on_instance_base() -> None:
    url = build_connect_url(_IMBUE, "z.example.com", "https://z.example.com/")
    qs = parse_qs(urlparse(url).query)
    # No doubled slash before the callback path.
    assert qs["callback"] == ["https://z.example.com/api/settings/connect-imbue/callback"]


def test_build_connect_url_strips_both_trailing_slashes() -> None:
    url = build_connect_url(_IMBUE + "/", "z.example.com", "https://z.example.com/")
    assert url.startswith(f"{_IMBUE}/connect/imbue?")
    qs = parse_qs(urlparse(url).query)
    assert qs["callback"] == ["https://z.example.com/api/settings/connect-imbue/callback"]


def test_build_connect_url_preserves_frontend_port() -> None:
    url = build_connect_url("https://imbue.local:8443", "z.example.com", "https://z.example.com")
    parsed = urlparse(url)
    assert parsed.netloc == "imbue.local:8443"
    assert parsed.path == "/connect/imbue"


def test_build_connect_url_preserves_instance_port_in_callback() -> None:
    url = build_connect_url(_IMBUE, "z.example.com", "https://z.example.com:18080")
    qs = parse_qs(urlparse(url).query)
    assert qs["callback"] == ["https://z.example.com:18080/api/settings/connect-imbue/callback"]


def test_build_connect_url_http_instance_base() -> None:
    url = build_connect_url(_IMBUE, "z.example.com", "http://z.example.com")
    qs = parse_qs(urlparse(url).query)
    assert qs["callback"] == ["http://z.example.com/api/settings/connect-imbue/callback"]


def test_build_connect_url_http_frontend() -> None:
    url = build_connect_url("http://imbue.local", "z.example.com", "https://z.example.com")
    assert url.startswith("http://imbue.local/connect/imbue?")


def test_build_connect_url_subdomain_zone() -> None:
    url = build_connect_url(_IMBUE, "team.alice.selfhost.imbue.com", "https://team.alice.selfhost.imbue.com")
    qs = parse_qs(urlparse(url).query)
    assert qs["zone"] == ["team.alice.selfhost.imbue.com"]


def test_build_connect_url_zone_with_special_chars_is_encoded() -> None:
    # A zone value with reserved characters must be percent-encoded, not injected
    # raw into the query string.
    url = build_connect_url(_IMBUE, "a b&c=d", "https://z.example.com")
    assert "zone=a b&c=d" not in url
    qs = parse_qs(urlparse(url).query)
    assert qs["zone"] == ["a b&c=d"]


def test_build_connect_url_frontend_with_existing_query_appends_start_path() -> None:
    # The builder appends the start path + a fresh query; it does not merge into a
    # pre-existing query on the frontend base. Pin the actual (documented) behavior.
    url = build_connect_url("https://imbue.local?ref=x", "z.example.com", "https://z.example.com")
    assert url.startswith("https://imbue.local?ref=x/connect/imbue?")


def test_build_connect_url_callback_uses_the_callback_path_constant() -> None:
    url = build_connect_url(_IMBUE, "z.example.com", "https://z.example.com")
    qs = parse_qs(urlparse(url).query)
    assert qs["callback"][0].endswith(CONNECT_CALLBACK_PATH)


# --- exchange_code_for_credential: mocking helper ----------------------------


def _mock_post(monkeypatch: pytest.MonkeyPatch, handler: Callable[[], httpx.Response]) -> dict[str, Any]:
    """Patch ``httpx.post`` with a handler; capture the call args in a dict."""
    seen: dict[str, Any] = {}

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> httpx.Response:
        seen["url"] = url
        seen["json"] = json
        seen["timeout"] = timeout
        return handler()

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def _ok_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "issuer_url": "https://kc/realms/openhost-customers",
        "client_id": "instance-alice",
        "client_secret": "sekret",
    }
    body.update(overrides)
    return body


# --- exchange: happy path ----------------------------------------------------


def test_exchange_returns_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    out = exchange_code_for_credential(_IMBUE, "one-time-code")
    assert out == KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


def test_exchange_posts_to_exchange_endpoint_with_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    exchange_code_for_credential(_IMBUE, "one-time-code")
    assert seen["url"] == f"{_IMBUE}/connect/imbue/exchange"
    assert seen["json"] == {"code": "one-time-code"}


def test_exchange_strips_trailing_slash_on_base(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    exchange_code_for_credential(_IMBUE + "/", "code")
    assert seen["url"] == f"{_IMBUE}/connect/imbue/exchange"


def test_exchange_default_timeout_is_30(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    exchange_code_for_credential(_IMBUE, "code")
    assert seen["timeout"] == 30.0


def test_exchange_custom_timeout_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    exchange_code_for_credential(_IMBUE, "code", timeout=5.0)
    assert seen["timeout"] == 5.0


def test_exchange_ignores_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _ok_body(zone_domain="alice.example.com", extra="ignored")
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=body))
    out = exchange_code_for_credential(_IMBUE, "code")
    assert out == KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


def test_exchange_coerces_non_string_fields_to_str(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exchange wraps each field in str(); pin that a numeric field is coerced.
    body = _ok_body(client_id=12345)
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=body))
    out = exchange_code_for_credential(_IMBUE, "code")
    assert out.client_id == "12345"


def test_exchange_accepts_empty_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # The helper itself does not validate the code (the route does); an empty code
    # is posted as-is and a 200 body yields a credential.
    seen = _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    exchange_code_for_credential(_IMBUE, "")
    assert seen["json"] == {"code": ""}


def test_exchange_accepts_large_code(monkeypatch: pytest.MonkeyPatch) -> None:
    big = "c" * 10000
    seen = _mock_post(monkeypatch, lambda: httpx.Response(200, json=_ok_body()))
    exchange_code_for_credential(_IMBUE, big)
    assert seen["json"] == {"code": big}


# --- exchange: missing fields -> ConnectError --------------------------------


def test_exchange_missing_issuer_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _ok_body()
    del body["issuer_url"]
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=body))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_missing_client_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _ok_body()
    del body["client_id"]
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=body))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_missing_client_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _ok_body()
    del body["client_secret"]
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=body))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_empty_object_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(200, json={}))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


# --- exchange: malformed / non-JSON bodies -----------------------------------


def test_exchange_non_json_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(200, text="not json at all"))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_json_list_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A JSON array (not a dict) -> indexing by key raises TypeError -> ConnectError.
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=["a", "b"]))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_json_null_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(200, json=None))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential(_IMBUE, "code")


# --- exchange: non-200 statuses ----------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
def test_exchange_non_200_raises_with_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(ConnectError, match=f"HTTP {status}") as ei:
        exchange_code_for_credential(_IMBUE, "code")
    assert "connect exchange failed" in str(ei.value)


def test_exchange_non_200_includes_error_message_from_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(400, json={"error": "code expired"}))
    with pytest.raises(ConnectError, match="code expired"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_non_200_non_json_body_uses_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_post(monkeypatch, lambda: httpx.Response(500, text="internal boom"))
    with pytest.raises(ConnectError, match="internal boom"):
        exchange_code_for_credential(_IMBUE, "code")


# --- exchange: network errors ------------------------------------------------


def test_exchange_connect_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_post(monkeypatch, boom)
    with pytest.raises(ConnectError, match="could not reach"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    _mock_post(monkeypatch, boom)
    with pytest.raises(ConnectError, match="could not reach"):
        exchange_code_for_credential(_IMBUE, "code")


def test_exchange_connect_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    _mock_post(monkeypatch, boom)
    with pytest.raises(ConnectError, match="could not reach"):
        exchange_code_for_credential(_IMBUE, "code")


# --- ConnectError itself -----------------------------------------------------


def test_connect_error_str_is_the_message() -> None:
    err = ConnectError("something broke")
    assert str(err) == "something broke"
    assert isinstance(err, Exception)
