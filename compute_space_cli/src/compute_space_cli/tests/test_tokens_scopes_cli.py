"""Tests for the ``oh tokens`` scope surface: create --scope and tokens scopes."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from compute_space_cli import config
from compute_space_cli.main import TokensCmd


def _instance() -> config.Instance:
    return config.Instance(hostname="example.com", token="tok")


def _created_response(scopes: list[str]) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "token": "raw-token-value",
        "token_id": "tok_abc123",
        "name": "t",
        "expires_at": None,
        "scopes": scopes,
    }
    return r


def test_create_defaults_to_owner_scope_when_none_given() -> None:
    # An unscoped `oh tokens create` sends the explicit ["owner"] default, since
    # the server requires scopes and has no implicit owner-on-omission.
    with patch(
        "compute_space_cli.main.make_api_request",
        return_value=_created_response(["owner"]),
    ) as mock_req:
        TokensCmd().create(cfg=_instance(), name="t", expiry_hours="8", scope=None)
    sent = mock_req.call_args.kwargs["data"]
    assert sent["scopes"] == ["owner"]


def test_create_forwards_explicit_scopes() -> None:
    with patch(
        "compute_space_cli.main.make_api_request",
        return_value=_created_response(["apps:read", "apps:logs"]),
    ) as mock_req:
        TokensCmd().create(
            cfg=_instance(),
            name="t",
            expiry_hours="8",
            scope=["apps:read", "apps:logs"],
        )
    sent = mock_req.call_args.kwargs["data"]
    assert sent["scopes"] == ["apps:read", "apps:logs"]


def test_tokens_scopes_fetches_and_prints_catalog(capsys) -> None:  # type: ignore[no-untyped-def]
    # `oh tokens scopes` renders the server-provided catalog (single source of
    # truth), including the owner-equivalent marker.
    catalog = [
        {"name": "owner", "description": "Full access (all scopes)", "owner_equivalent": True},
        {"name": "apps:read", "description": "List apps, status, diagnostics", "owner_equivalent": False},
    ]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = catalog
    with patch("compute_space_cli.main.make_api_request", return_value=resp) as mock_req:
        TokensCmd().scopes(cfg=_instance())
    # Fetched from the catalog endpoint.
    called_path = mock_req.call_args.args[3] if len(mock_req.call_args.args) > 3 else None
    assert called_path == "/api/token_scopes"
    out = capsys.readouterr().out
    assert "owner" in out
    assert "apps:read" in out
    assert "owner-equivalent" in out
