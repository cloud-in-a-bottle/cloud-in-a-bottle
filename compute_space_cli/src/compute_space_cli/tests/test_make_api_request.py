"""Tests for :func:`compute_space_cli.helpers.make_api_request`."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from compute_space_cli.helpers import authorization_url
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
        ({"extra": {"authorize_url": "//oauth"}}, "//oauth"),
        ({"authorize_url": "//legacy-oauth"}, "//legacy-oauth"),
        ({"extra": {"authorize_url": 123}}, None),
        ({}, None),
    ],
)
def test_authorization_url_reads_current_and_legacy_envelopes(body: Any, expected: str | None) -> None:
    assert authorization_url(body) == expected
