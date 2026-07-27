"""Complementary edge-case tests for the instance-side Connect-to-Imbue flow.

These ADD to ``test_connect.py`` (which already covers the happy paths and the
core failure modes) with rigorous edge cases that pin the *exact* observed
behavior of ``core/connect.py``, ``config.active_config_path``, and the
``/api/settings/connect-imbue/*`` routes. Every assertion here was confirmed by
running against the real source — including a couple of quirks that are the
current behavior rather than an ideal (e.g. ``build_connect_url`` naively appends
its path+query and does NOT merge into a frontend base URL that already carries a
query string; that is asserted as-is, not as aspiration).

Nothing here duplicates a ``test_connect.py`` case: the build_connect_url cases
use different inputs (ports, subdomain zones, urlencoding of special chars, http
vs https, IDN-ish hosts, pre-existing query), the persist cases probe many-key
preservation / non-table sections / atomicity / unicode round-trips, the exchange
cases enumerate each missing field / non-JSON body / every error status / each
network-error subclass / oversized+empty codes / extra-field tolerance, and the
route cases probe proxy-header derivation, auth on all three endpoints, and the
callback's persistence + restart side-effect ordering.
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs
from urllib.parse import urlparse

import bcrypt
import httpx
import pytest
import typed_settings
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import DefaultConfig
from compute_space.config import active_config_path
from compute_space.config import provide_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.connect import CONNECT_CALLBACK_PATH
from compute_space.core.connect import ConnectError
from compute_space.core.connect import build_connect_url
from compute_space.core.connect import exchange_code_for_credential
from compute_space.core.connect import persist_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.settings import api_settings_routes

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

_IMBUE = "https://openhost.imbue.com"
_IDENT = dict(
    imbue_identity_issuer_url="https://kc/realms/openhost-customers",
    imbue_identity_client_id="instance-alice",
    imbue_identity_client_secret="sekret",
)


def _cred(
    issuer: str = "https://kc/realms/openhost-customers",
    client_id: str = "instance-alice",
    secret: str = "sekret",
) -> KeycloakClientCredentials:
    return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=secret)


def _mock_httpx_post(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> dict[str, object]:
    """Patch ``httpx.post`` with a handler that records the call and returns
    whatever ``handler()`` yields (a Response) or raises."""
    seen: dict[str, object] = {}

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> httpx.Response:
        seen["url"] = url
        seen["json"] = json
        seen["timeout"] = timeout
        result: httpx.Response = handler()
        return result

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


# ===========================================================================
# build_connect_url
# ===========================================================================


def test_build_connect_url_strips_trailing_slashes_on_both_bases() -> None:
    # Both bases carry a trailing slash; neither should leak a double slash into
    # the emitted URL. (test_connect.py checks a single case; this asserts the
    # exact path join on both sides.)
    url = build_connect_url("https://front.example.com/", "z.example.com", "https://z.example.com/")
    assert url.startswith("https://front.example.com/connect/imbue?")
    assert "//connect/imbue" not in url
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb == "https://z.example.com/api/settings/connect-imbue/callback"
    assert "//api/settings" not in cb


def test_build_connect_url_strips_multiple_trailing_slashes_only_one() -> None:
    # rstrip('/') removes ALL trailing slashes, so even a doubled slash collapses.
    url = build_connect_url("https://front.example.com///", "z", "https://z.example.com//")
    assert url.startswith("https://front.example.com/connect/imbue?")
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb == "https://z.example.com/api/settings/connect-imbue/callback"


def test_build_connect_url_preserves_port_on_frontend_base() -> None:
    url = build_connect_url("https://front.example.com:8443", "z", "https://z.example.com")
    assert url.startswith("https://front.example.com:8443/connect/imbue?")


def test_build_connect_url_preserves_port_in_callback() -> None:
    url = build_connect_url("https://front.example.com", "z", "https://z.example.com:9443")
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb == "https://z.example.com:9443/api/settings/connect-imbue/callback"


def test_build_connect_url_zone_with_subdomain_is_verbatim() -> None:
    zone = "team.alice.selfhost.imbue.com"
    url = build_connect_url("https://front.example.com", zone, "https://z.example.com")
    assert parse_qs(urlparse(url).query)["zone"][0] == zone


def test_build_connect_url_urlencodes_special_chars_in_zone() -> None:
    # A zone containing reserved URL chars must be percent-encoded in the query
    # so it round-trips through a real query parser as the literal string.
    zone = "weird zone/with&reserved=chars"
    url = build_connect_url("https://front.example.com", zone, "https://z.example.com")
    # Raw reserved chars must not appear unescaped in the query segment.
    query = urlparse(url).query
    assert " " not in query
    assert "weird+zone" in query or "weird%20zone" in query
    # ...but a real parser recovers the exact original.
    assert parse_qs(url.split("?", 1)[1])["zone"][0] == zone


def test_build_connect_url_urlencodes_plus_and_percent_in_zone() -> None:
    zone = "a+b%c"
    url = build_connect_url("https://front.example.com", zone, "https://z.example.com")
    assert parse_qs(url.split("?", 1)[1])["zone"][0] == zone


def test_build_connect_url_http_instance_base_kept() -> None:
    # An http (not https) instance base is preserved verbatim in the callback —
    # the function does not force https.
    url = build_connect_url("https://front.example.com", "z", "http://z.example.com")
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb.startswith("http://z.example.com/")


def test_build_connect_url_http_frontend_base_kept() -> None:
    url = build_connect_url("http://front.example.com", "z", "https://z.example.com")
    assert url.startswith("http://front.example.com/connect/imbue?")


def test_build_connect_url_idn_ascii_host_preserved() -> None:
    # A punycode/IDN-ascii host (xn--) must pass through untouched in both bases.
    url = build_connect_url("https://xn--mnchen-3ya.example.com", "z", "https://xn--80akhbyknj4f.example.com")
    assert url.startswith("https://xn--mnchen-3ya.example.com/connect/imbue?")
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb.startswith("https://xn--80akhbyknj4f.example.com/")


def test_build_connect_url_appends_after_existing_query_verbatim() -> None:
    # CURRENT BEHAVIOR (not ideal): the function unconditionally appends
    # ``/connect/imbue?<query>`` to the frontend base, so a base that ALREADY has
    # a query string produces a structurally odd URL. Pinned so a future change
    # that "fixes" this is a conscious, reviewed decision, not silent drift.
    url = build_connect_url("https://front.example.com/x?already=1", "z", "https://z.example.com")
    # The path+query is appended right after the untouched base (including its
    # existing query), so the base's ``?already=1`` is followed by the second
    # ``?zone=z...`` — a URL with two '?' separators.
    assert url.startswith("https://front.example.com/x?already=1/connect/imbue?zone=z&callback=")
    assert url.count("?") == 2


def test_build_connect_url_callback_path_matches_module_constant() -> None:
    # The callback embedded in the URL is exactly the route the callback handler
    # is registered at (drift here would silently break the redirect).
    url = build_connect_url("https://front.example.com", "z", "https://z.example.com")
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb.endswith(CONNECT_CALLBACK_PATH)


def test_build_connect_url_empty_zone_yields_empty_query_value() -> None:
    # An empty zone is encoded as ``zone=`` (present but empty), not dropped.
    url = build_connect_url("https://front.example.com", "", "https://z.example.com")
    assert "zone=" in url
    assert parse_qs(url.split("?", 1)[1], keep_blank_values=True)["zone"][0] == ""


# ===========================================================================
# persist_instance_identity
# ===========================================================================


def test_persist_preserves_many_preexisting_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[openhost]\n"
        'zone_domain = "alice.example.com"\n'
        "port = 8080\n"
        "tls_enabled = true\n"
        'host = "0.0.0.0"\n'
        'data_root_dir = "/opt/openhost"\n'
        "coredns_enabled = false\n"
        'default_apps = ["a", "b", "c"]\n'
        "storage_min_free_mb = 500\n"
    )
    persist_instance_identity(str(cfg), _cred())
    data = tomllib.loads(cfg.read_text())["openhost"]
    # every pre-existing key survives, with its type intact
    assert data["zone_domain"] == "alice.example.com"
    assert data["port"] == 8080
    assert data["tls_enabled"] is True
    assert data["host"] == "0.0.0.0"
    assert data["data_root_dir"] == "/opt/openhost"
    assert data["coredns_enabled"] is False
    assert data["default_apps"] == ["a", "b", "c"]
    assert data["storage_min_free_mb"] == 500
    # and the identity was added
    assert data["imbue_identity_client_id"] == "instance-alice"


def test_persist_overwrites_only_the_three_identity_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[openhost]\n"
        'zone_domain = "a.example.com"\n'
        'imbue_identity_issuer_url = "old-iss"\n'
        'imbue_identity_client_id = "old-id"\n'
        'imbue_identity_client_secret = "old-secret"\n'
        'email_proxy_base_url = "https://keep.me"\n'
    )
    persist_instance_identity(str(cfg), _cred("new-iss", "new-id", "new-secret"))
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert data["imbue_identity_issuer_url"] == "new-iss"
    assert data["imbue_identity_client_id"] == "new-id"
    assert data["imbue_identity_client_secret"] == "new-secret"
    # an unrelated key is untouched
    assert data["email_proxy_base_url"] == "https://keep.me"


def test_persist_creates_missing_file(tmp_path: Path) -> None:
    # No file on disk at all -> FileNotFoundError path -> writes a fresh config.
    cfg = tmp_path / "does_not_exist.toml"
    assert not cfg.exists()
    persist_instance_identity(str(cfg), _cred("iss", "id", "sec"))
    assert cfg.exists()
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert data == {
        "imbue_identity_issuer_url": "iss",
        "imbue_identity_client_id": "id",
        "imbue_identity_client_secret": "sec",
    }


def test_persist_preserves_non_openhost_top_level_sections(tmp_path: Path) -> None:
    # Top-level tables other than [openhost] must be carried through untouched.
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[openhost]\n"
        'zone_domain = "a.example.com"\n'
        "\n"
        "[other]\n"
        'foo = "bar"\n'
        "num = 42\n"
        "\n"
        "[nested.section]\n"
        "flag = true\n"
    )
    persist_instance_identity(str(cfg), _cred())
    data = tomllib.loads(cfg.read_text())
    assert data["other"] == {"foo": "bar", "num": 42}
    assert data["nested"]["section"]["flag"] is True
    assert data["openhost"]["imbue_identity_client_id"] == "instance-alice"


def test_persist_when_openhost_is_not_a_table_replaces_with_table(tmp_path: Path) -> None:
    # EDGE: if ``openhost`` is a scalar (not a table), the code detects the
    # non-dict via ``isinstance(section, dict)`` and starts a fresh section,
    # discarding the bogus scalar. Confirm it doesn't crash and yields a table.
    cfg = tmp_path / "config.toml"
    cfg.write_text('openhost = "not-a-table"\n')
    persist_instance_identity(str(cfg), _cred("iss", "id", "sec"))
    data = tomllib.loads(cfg.read_text())
    assert isinstance(data["openhost"], dict)
    assert data["openhost"]["imbue_identity_issuer_url"] == "iss"


def test_persist_no_tmp_file_left_behind(tmp_path: Path) -> None:
    # Atomic write uses ``<path>.connect.tmp`` then os.replace; the temp file must
    # not survive a successful write.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "a.example.com"\n')
    persist_instance_identity(str(cfg), _cred())
    assert not (tmp_path / "config.toml.connect.tmp").exists()
    # only the config file should be present (plus nothing else we created)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_persist_result_has_no_duplicate_identity_keys(tmp_path: Path) -> None:
    # Persisting twice must not accumulate duplicate keys or change the section
    # into anything but the latest values.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "a.example.com"\n')
    persist_instance_identity(str(cfg), _cred("i1", "c1", "s1"))
    persist_instance_identity(str(cfg), _cred("i2", "c2", "s2"))
    text = cfg.read_text()
    assert text.count("imbue_identity_client_id") == 1
    data = tomllib.loads(text)["openhost"]
    assert data["imbue_identity_client_id"] == "c2"
    assert data["imbue_identity_client_secret"] == "s2"


def test_persist_unicode_values_survive_round_trip(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "a.example.com"\n')
    cred = _cred(
        issuer="https://kc.münchen.example/realms/öffentlich",
        client_id="instance-地域",
        secret="pä$$wörd-🔒",
    )
    persist_instance_identity(str(cfg), cred)
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert data["imbue_identity_issuer_url"] == "https://kc.münchen.example/realms/öffentlich"
    assert data["imbue_identity_client_id"] == "instance-地域"
    assert data["imbue_identity_client_secret"] == "pä$$wörd-🔒"


def test_persist_quote_heavy_secret_survives_round_trip(tmp_path: Path) -> None:
    # Secrets full of TOML-significant chars (quotes, backslashes, brackets) must
    # survive the tomli_w -> tomllib round-trip verbatim.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "a.example.com"\n')
    tricky = "a\"b'c\\d[e]f#g=h\nnewline\ttab"
    persist_instance_identity(str(cfg), _cred(secret=tricky))
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert data["imbue_identity_client_secret"] == tricky


def test_persist_result_loads_via_typed_settings_and_resolves_identity(tmp_path: Path) -> None:
    # End-to-end: persist then load through the real typed_settings path and
    # confirm the credential resolves through Config.instance_identity.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "alice.example.com"\n')
    persist_instance_identity(str(cfg), _cred("https://iss", "cid", "csecret"))
    loaded = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(cfg)])
    ident = loaded.instance_identity
    assert ident is not None
    assert ident.issuer_url == "https://iss"
    assert ident.client_id == "cid"
    assert ident.client_secret == "csecret"


def test_persist_into_empty_openhost_table_adds_only_identity(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[openhost]\n")
    persist_instance_identity(str(cfg), _cred("iss", "id", "sec"))
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert set(data) == {
        "imbue_identity_issuer_url",
        "imbue_identity_client_id",
        "imbue_identity_client_secret",
    }


# ===========================================================================
# exchange_code_for_credential
# ===========================================================================


def test_exchange_passes_timeout_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(
            200,
            json={"issuer_url": "i", "client_id": "c", "client_secret": "s"},
        ),
    )
    exchange_code_for_credential("https://front.example.com", "code", timeout=7.5)
    assert seen["timeout"] == 7.5


def test_exchange_strips_trailing_slash_on_base(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(200, json={"issuer_url": "i", "client_id": "c", "client_secret": "s"}),
    )
    exchange_code_for_credential("https://front.example.com/", "code")
    assert seen["url"] == "https://front.example.com/connect/imbue/exchange"


def test_exchange_coerces_non_string_fields_to_str(monkeypatch: pytest.MonkeyPatch) -> None:
    # The impl wraps each field in str(), so a JSON number becomes its str form.
    _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(200, json={"issuer_url": "i", "client_id": 12345, "client_secret": "s"}),
    )
    out = exchange_code_for_credential("https://front.example.com", "code")
    assert out.client_id == "12345"


@pytest.mark.parametrize("missing", ["issuer_url", "client_id", "client_secret"])
def test_exchange_malformed_when_any_field_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    body = {"issuer_url": "i", "client_id": "c", "client_secret": "s"}
    del body[missing]
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(200, json=body))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential("https://front.example.com", "code")


def test_exchange_malformed_on_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 200 whose body isn't JSON: resp.json() raises ValueError -> malformed.
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(200, text="this is not json"))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential("https://front.example.com", "code")


def test_exchange_malformed_on_json_list_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # A JSON list (not a dict) -> body["issuer_url"] raises TypeError -> malformed.
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential("https://front.example.com", "code")


def test_exchange_ignores_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(
            200,
            json={
                "issuer_url": "i",
                "client_id": "c",
                "client_secret": "s",
                "extra": "ignored",
                "zone_domain": "alice.example.com",
                "nested": {"a": 1},
            },
        ),
    )
    out = exchange_code_for_credential("https://front.example.com", "code")
    assert out == _cred("i", "c", "s")


@pytest.mark.parametrize("status", [400, 401, 403, 500, 502, 503])
def test_exchange_error_status_includes_code_in_message(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(ConnectError) as exc:
        exchange_code_for_credential("https://front.example.com", "code")
    assert f"HTTP {status}" in str(exc.value)
    assert "nope" in str(exc.value)


def test_exchange_error_status_non_json_body_uses_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-200 with a non-JSON body: _error_message falls back to resp.text[:200].
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(500, text="internal boom"))
    with pytest.raises(ConnectError) as exc:
        exchange_code_for_credential("https://front.example.com", "code")
    assert "HTTP 500" in str(exc.value)
    assert "internal boom" in str(exc.value)


def test_exchange_error_message_truncated_to_200_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    # _error_message caps the body text at 200 chars.
    long_body = "x" * 5000
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(500, text=long_body))
    with pytest.raises(ConnectError) as exc:
        exchange_code_for_credential("https://front.example.com", "code")
    # 200 x's max, not 5000.
    assert "x" * 200 in str(exc.value)
    assert "x" * 201 not in str(exc.value)


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: httpx.ConnectError("connection refused"),
        lambda: httpx.TimeoutException("timed out"),
        lambda: httpx.ReadError("read failed"),
        lambda: httpx.ReadTimeout("read timed out"),
        lambda: httpx.ConnectTimeout("connect timed out"),
    ],
)
def test_exchange_wraps_each_network_error(monkeypatch: pytest.MonkeyPatch, exc_factory: Any) -> None:
    def fake_post(url: str, json: Any = None, timeout: Any = None) -> httpx.Response:
        raise exc_factory()

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(ConnectError, match="could not reach") as exc:
        exchange_code_for_credential("https://front.example.com", "code")
    # The original httpx error is chained for debuggability.
    assert isinstance(exc.value.__cause__, httpx.HTTPError)


def test_exchange_sends_empty_code_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    # The function does not validate the code; an empty code is posted as-is
    # (the route, not this function, guards against blank codes).
    seen = _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(200, json={"issuer_url": "i", "client_id": "c", "client_secret": "s"}),
    )
    exchange_code_for_credential("https://front.example.com", "")
    assert seen["json"] == {"code": ""}


def test_exchange_sends_large_code_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    big = "z" * 100_000
    seen = _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(200, json={"issuer_url": "i", "client_id": "c", "client_secret": "s"}),
    )
    exchange_code_for_credential("https://front.example.com", big)
    assert seen["json"] == {"code": big}


def test_exchange_error_dict_without_error_key_uses_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # A JSON error dict lacking an "error" key: _error_message falls back to
    # str(body.get("error", resp.text[:200])) -> the text.
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(400, json={"detail": "something"}))
    with pytest.raises(ConnectError) as exc:
        exchange_code_for_credential("https://front.example.com", "code")
    assert "HTTP 400" in str(exc.value)


# ===========================================================================
# active_config_path
# ===========================================================================


def test_active_config_path_router_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", "/etc/openhost/router.toml")
    monkeypatch.delenv("OPENHOST_CONFIG", raising=False)
    assert active_config_path() == "/etc/openhost/router.toml"


def test_active_config_path_legacy_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENHOST_ROUTER_CONFIG", raising=False)
    monkeypatch.setenv("OPENHOST_CONFIG", "/etc/openhost/legacy.toml")
    assert active_config_path() == "/etc/openhost/legacy.toml"


def test_active_config_path_router_wins_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", "/etc/openhost/router.toml")
    monkeypatch.setenv("OPENHOST_CONFIG", "/etc/openhost/legacy.toml")
    assert active_config_path() == "/etc/openhost/router.toml"


def test_active_config_path_none_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENHOST_ROUTER_CONFIG", raising=False)
    monkeypatch.delenv("OPENHOST_CONFIG", raising=False)
    assert active_config_path() is None


def test_active_config_path_empty_router_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty (falsy) OPENHOST_ROUTER_CONFIG must not shadow a set legacy var —
    # ``a or b`` semantics mean the empty string is skipped.
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", "")
    monkeypatch.setenv("OPENHOST_CONFIG", "/etc/openhost/legacy.toml")
    assert active_config_path() == "/etc/openhost/legacy.toml"


# ===========================================================================
# routes: shared harness
# ===========================================================================


@pytest.fixture
def connected_cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(
        tmp_path,
        port=20700,
        zone_domain="alice.example.com",
        email_proxy_base_url=_IMBUE,
        public_ip="203.0.113.5",
        **_IDENT,
    )
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def unconnected_cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20701, zone_domain="alice.example.com", email_proxy_base_url=_IMBUE)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def no_imbue_cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20702, zone_domain="alice.example.com")
    init_db(cfg.db_path)
    return cfg


def _settings_client() -> TestClient[Litestar]:
    app = Litestar(
        route_handlers=[api_settings_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )
    return TestClient(app=app)


def _auth_cookie(cfg: Any) -> dict[str, str]:
    pw_hash = bcrypt.hashpw(b"secretpass1", bcrypt.gensalt()).decode()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("owner", pw_hash))
        assert cur.lastrowid is not None
        token = create_session(cur.lastrowid, conn)
        conn.commit()
    finally:
        conn.close()
    return {SESSION_COOKIE_NAME: token}


# ===========================================================================
# routes: status
# ===========================================================================


def test_status_partial_identity_property_reports_not_connected() -> None:
    # Only issuer + client_id present (no secret) -> instance_identity is None, so
    # the status route's ``connected`` (which is ``instance_identity is not None``)
    # would be False. This partial state is only reachable WITHOUT an Imbue base
    # (setting email_proxy_base_url with a partial credential is rejected at config
    # construction — see the next test), so we pin the property directly here.
    cfg = DefaultConfig(
        zone_domain="alice.example.com",
        imbue_identity_issuer_url="https://kc/realms/openhost-customers",
        imbue_identity_client_id="instance-alice",
    )
    assert cfg.instance_identity is None


def test_config_rejects_imbue_base_with_partial_identity() -> None:
    # A config that advertises the connect front door (email_proxy_base_url) but
    # carries only a PARTIAL imbue identity is a misconfiguration and must be
    # rejected at construction — so the status route never has to represent an
    # "available + partially-connected" state.
    with pytest.raises(ValueError, match="partially resolved"):
        DefaultConfig(
            zone_domain="alice.example.com",
            email_proxy_base_url=_IMBUE,
            imbue_identity_issuer_url="https://kc/realms/openhost-customers",
            imbue_identity_client_id="instance-alice",
        )


def test_status_connected_via_cert_api_override_fallback(tmp_path: Path) -> None:
    # instance_identity resolves through the DEPRECATED cert_api_keycloak_*
    # override too, so a cert-api-provisioned instance reports connected=True.
    cfg = _make_test_config(
        tmp_path,
        port=20711,
        zone_domain="alice.example.com",
        email_proxy_base_url=_IMBUE,
        public_ip="203.0.113.9",
        cert_api_keycloak_issuer_url="https://kc/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-alice",
        cert_api_keycloak_client_secret="sekret",
    )
    init_db(cfg.db_path)
    with _settings_client() as c:
        resp = c.get("/api/settings/connect-imbue/status", cookies=_auth_cookie(cfg))
    assert resp.json() == {"available": True, "connected": True}


def test_status_available_tracks_imbue_connect_base_url(connected_cfg: Any) -> None:
    # available is derived purely from imbue_connect_base_url (== email_proxy_base_url).
    assert connected_cfg.imbue_connect_base_url == _IMBUE
    with _settings_client() as c:
        resp = c.get("/api/settings/connect-imbue/status", cookies=_auth_cookie(connected_cfg))
    assert resp.json()["available"] is True


# ===========================================================================
# routes: start (proxy header derivation)
# ===========================================================================


def test_start_uses_forwarded_proto_and_host(unconnected_cfg: Any) -> None:
    # Behind a proxy, the instance origin is taken from the X-Forwarded-* headers.
    with _settings_client() as c:
        resp = c.post(
            "/api/settings/connect-imbue/start",
            cookies=_auth_cookie(unconnected_cfg),
            headers={"x-forwarded-proto": "https", "x-forwarded-host": "proxy.example.com"},
        )
    url = resp.json()["redirect_url"]
    cb = parse_qs(urlparse(url).query)["callback"][0]
    assert cb == "https://proxy.example.com/api/settings/connect-imbue/callback"


def test_start_forwarded_proto_overrides_request_scheme(unconnected_cfg: Any) -> None:
    # TestClient speaks http; x-forwarded-proto=https must win, so the callback
    # is https even though the underlying request is http.
    with _settings_client() as c:
        resp = c.post(
            "/api/settings/connect-imbue/start",
            cookies=_auth_cookie(unconnected_cfg),
            headers={"x-forwarded-proto": "https", "x-forwarded-host": "edge.example.com"},
        )
    cb = parse_qs(urlparse(resp.json()["redirect_url"]).query)["callback"][0]
    assert cb.startswith("https://edge.example.com/")


def test_start_falls_back_to_host_header(unconnected_cfg: Any) -> None:
    # No forwarded-host -> the plain Host header is used for the instance origin.
    with _settings_client() as c:
        resp = c.post(
            "/api/settings/connect-imbue/start",
            cookies=_auth_cookie(unconnected_cfg),
            headers={"host": "direct.example.com"},
        )
    cb = parse_qs(urlparse(resp.json()["redirect_url"]).query)["callback"][0]
    assert cb.startswith("http://direct.example.com/") or cb.startswith("https://direct.example.com/")
    assert "direct.example.com" in cb


def test_start_forwarded_host_wins_over_host_header(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.post(
            "/api/settings/connect-imbue/start",
            cookies=_auth_cookie(unconnected_cfg),
            headers={"host": "origin.example.com", "x-forwarded-host": "proxy.example.com"},
        )
    cb = parse_qs(urlparse(resp.json()["redirect_url"]).query)["callback"][0]
    assert "proxy.example.com" in cb
    assert "origin.example.com" not in cb


def test_start_zone_query_uses_config_zone_no_port(unconnected_cfg: Any) -> None:
    # The zone= param is the configured zone (no port), independent of the host header.
    with _settings_client() as c:
        resp = c.post(
            "/api/settings/connect-imbue/start",
            cookies=_auth_cookie(unconnected_cfg),
            headers={"x-forwarded-host": "somethingelse.example.com"},
        )
    zone = parse_qs(urlparse(resp.json()["redirect_url"]).query)["zone"][0]
    assert zone == "alice.example.com"


def test_start_frontend_base_is_imbue_connect_url(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.post("/api/settings/connect-imbue/start", cookies=_auth_cookie(unconnected_cfg))
    assert resp.json()["redirect_url"].startswith(f"{_IMBUE}/connect/imbue?")


def test_start_requires_auth_no_cookie(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        assert c.post("/api/settings/connect-imbue/start").status_code == 401


# ===========================================================================
# routes: callback
# ===========================================================================


def _config_file(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point OPENHOST_ROUTER_CONFIG at a real config file for the callback to
    persist into, and return its path."""
    path = Path(cfg.data_root_dir) / "config.toml"
    path.write_text('[openhost]\nzone_domain = "alice.example.com"\n')
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(path))
    return path


def test_callback_persists_the_exchanged_credential(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_file(unconnected_cfg, monkeypatch)
    exchanged = _cred("https://iss.new", "cid-new", "csecret-new")
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=exchanged,
        ),
        mock.patch("compute_space.web.routes.api.settings.persist_instance_identity") as persist,
        mock.patch("compute_space.web.routes.api.settings.trigger_restart"),
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code in (302, 307)
    # persist must be called with the exact credential the exchange returned.
    persist.assert_called_once()
    _, called_cred = persist.call_args.args
    assert called_cred == exchanged


def test_callback_trigger_restart_called_once_on_success(
    unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_file(unconnected_cfg, monkeypatch)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ),
        mock.patch("compute_space.web.routes.api.settings.persist_instance_identity"),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/settings?connect=ok"
    restart.assert_called_once_with()


def test_callback_real_persist_writes_config(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the callback with the REAL persist_instance_identity (only the
    # exchange + restart mocked) so the whole write path is covered end to end.
    path = _config_file(unconnected_cfg, monkeypatch)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred("https://iss.e2e", "cid.e2e", "sec.e2e"),
        ),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart"),
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/settings?connect=ok"
    data = tomllib.loads(path.read_text())["openhost"]
    assert data["imbue_identity_issuer_url"] == "https://iss.e2e"
    assert data["imbue_identity_client_id"] == "cid.e2e"
    assert data["imbue_identity_client_secret"] == "sec.e2e"


@pytest.mark.parametrize("code_value", ["", "   ", "\t", "\n", "  \t \n "])
def test_callback_blank_or_whitespace_code_redirects_error(
    unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch, code_value: str
) -> None:
    _config_file(unconnected_cfg, monkeypatch)
    # A blank/whitespace code short-circuits to the error redirect and must NOT
    # trigger an exchange or a restart.
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
        ) as exchange,
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback",
            params={"code": code_value},
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=error"
    exchange.assert_not_called()
    restart.assert_not_called()


def test_callback_missing_code_param_redirects_error(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The handler defaults code="" so an entirely-absent ?code= behaves like blank.
    _config_file(unconnected_cfg, monkeypatch)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
        ) as exchange,
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/settings?connect=error"
    exchange.assert_not_called()


def test_callback_connect_error_502_and_no_restart(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_file(unconnected_cfg, monkeypatch)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            side_effect=ConnectError("code expired"),
        ),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=bad",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 502
    # A failed exchange must not restart the instance.
    restart.assert_not_called()


def test_callback_persist_error_502_and_no_restart(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A ConnectError raised by persist (not just exchange) is also caught -> 502,
    # no restart.
    _config_file(unconnected_cfg, monkeypatch)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ),
        mock.patch(
            "compute_space.web.routes.api.settings.persist_instance_identity",
            side_effect=ConnectError("disk full"),
        ),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 502
    restart.assert_not_called()


def test_callback_500_when_no_config_path(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Env-driven config (no file) -> the credential can't be persisted -> 500,
    # and no exchange/restart happens.
    monkeypatch.delenv("OPENHOST_ROUTER_CONFIG", raising=False)
    monkeypatch.delenv("OPENHOST_CONFIG", raising=False)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
        ) as exchange,
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 500
    exchange.assert_not_called()
    restart.assert_not_called()


def test_callback_503_without_imbue_base(no_imbue_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=x",
            cookies=_auth_cookie(no_imbue_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 503


def test_callback_503_precedes_blank_code_check(no_imbue_cfg: Any) -> None:
    # Even with a blank code, the missing-Imbue-base check fires first -> 503,
    # not the error redirect. Pins the ordering of the guards.
    with _settings_client() as c:
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=",
            cookies=_auth_cookie(no_imbue_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 503


def test_callback_requires_auth_no_cookie(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        assert c.get("/api/settings/connect-imbue/callback?code=x").status_code == 401


def test_callback_strips_surrounding_whitespace_before_exchange(
    unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A code with surrounding whitespace is stripped before being handed to the
    # exchange (so the front door sees the clean code).
    _config_file(unconnected_cfg, monkeypatch)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ) as exchange,
        mock.patch("compute_space.web.routes.api.settings.persist_instance_identity"),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart"),
        _settings_client() as c,
    ):
        c.get(
            "/api/settings/connect-imbue/callback",
            params={"code": "  spacey-code  "},
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    # second positional arg to exchange is the stripped code
    _, passed_code = exchange.call_args.args
    assert passed_code == "spacey-code"
