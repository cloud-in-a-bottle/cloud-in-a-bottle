"""Tests for the ``cb diagnostics`` and ``cb app diagnostics`` CLI commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from unittest.mock import patch

from compute_space_cli import config
from compute_space_cli.main import AppCmd
from compute_space_cli.main import Diagnostics


def _instance() -> config.Instance:
    return config.Instance(hostname="x", token="tok", alias="x")


def _resp(payload: dict[str, object]) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def test_platform_diagnostics_hits_endpoint_and_prints(capsys: object) -> None:
    payload = {"schema_version": 1, "zone_domain": "x"}
    with patch("compute_space_cli.main.make_api_request", return_value=_resp(payload)) as mock_req:
        Diagnostics()(_instance())
    mock_req.assert_called_once()
    args = mock_req.call_args.args
    assert args[2] == "GET"
    assert args[3] == "/api/diagnostics"
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert json.loads(out) == payload


def test_app_diagnostics_resolves_id_and_hits_endpoint(capsys: object) -> None:
    payload = {"schema_version": 1, "name": "myapp"}
    with (
        patch("compute_space_cli.main.resolve_app_id_by_name", return_value="APPID123"),
        patch("compute_space_cli.main.make_api_request", return_value=_resp(payload)) as mock_req,
    ):
        AppCmd().diagnostics("myapp", _instance())
    args = mock_req.call_args.args
    assert args[2] == "GET"
    assert args[3] == "/api/app_diagnostics/APPID123"
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert json.loads(out) == payload
