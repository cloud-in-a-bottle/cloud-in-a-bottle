"""Tests for :func:`compute_space_cli.helpers.make_api_request` and its error parsing."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from compute_space_cli.helpers import authorization_url
from compute_space_cli.helpers import error_message
from compute_space_cli.helpers import make_api_request


def _ok_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {}
    return r


def test_post_with_data_sends_json_body() -> None:
    with patch(
        "compute_space_cli.helpers.httpx.request",
        return_value=_ok_response(),
    ) as mock_req:
        make_api_request("https://x", "tok", "POST", "/api/foo", data={"k": "v"})
        kwargs = mock_req.call_args.kwargs
        assert kwargs.get("json") == {"k": "v"}
        assert "data" not in kwargs


def test_get_without_data_sends_no_body() -> None:
    with patch(
        "compute_space_cli.helpers.httpx.request",
        return_value=_ok_response(),
    ) as mock_req:
        make_api_request("https://x", "tok", "GET", "/api/foo")
        kwargs = mock_req.call_args.kwargs
        assert kwargs.get("json") is None
        assert "data" not in kwargs


def test_bearer_header_attached() -> None:
    with patch(
        "compute_space_cli.helpers.httpx.request",
        return_value=_ok_response(),
    ) as mock_req:
        make_api_request("https://x", "tok-123", "GET", "/api/foo")
        kwargs = mock_req.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer tok-123"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status_code": 400, "detail": "Invalid app name"}, "Invalid app name"),
        ({"status_code": 503, "detail": "no provider", "extra": {"code": "x"}}, "no provider"),
        # litestar replaces `detail` on 500s, so the reason has to come from `extra`
        ({"status_code": 500, "detail": "Internal Server Error", "extra": {"output": "docker died"}}, "docker died"),
        ({"error": "Not authorized"}, "Not authorized"),
        ({}, "fallback"),
        (["not", "a", "dict"], "fallback"),
    ],
)
def test_error_message_reads_displayable_reason(body: Any, expected: str) -> None:
    assert error_message(body, "fallback") == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"extra": {"authorize_url": "//oauth"}}, "//oauth"),
        ({"authorize_url": "//legacy-oauth"}, "//legacy-oauth"),
        ({"extra": {"authorize_url": 123}}, None),
        ({}, None),
    ],
)
def test_authorization_url_reads_current_and_legacy_envelopes(body: Any, expected: str | None) -> None:
    assert authorization_url(body) == expected
