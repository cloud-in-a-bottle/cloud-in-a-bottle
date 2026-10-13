"""Tests for :func:`compute_space_cli.discovery.discover_router_url`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import httpx
import pytest

from compute_space_cli import config as cli_config
from compute_space_cli.config import Instance
from compute_space_cli.config import MultiConfig
from compute_space_cli.discovery import DiscoveryError
from compute_space_cli.discovery import discover_router_url
from compute_space_cli.main import resolve_api_instance


def _response(status_code: int, body: Any = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    if isinstance(body, Exception):
        r.json.side_effect = body
    else:
        r.json.return_value = body
    return r


def _discover(response: MagicMock, base_url: str = "https://example.com") -> tuple[str | None, MagicMock]:
    with patch("compute_space_cli.discovery.httpx.get", return_value=response) as mock_get:
        return discover_router_url(base_url), mock_get


def test_returns_discovered_router_url_without_sending_a_token() -> None:
    router_url, mock_get = _discover(_response(200, {"router_url": "https://dash.example.com/"}))
    assert router_url == "https://dash.example.com"
    assert mock_get.call_args.args == ("https://example.com/.well-known/cloud-in-a-bottle",)
    assert "headers" not in mock_get.call_args.kwargs


def test_accepts_the_queried_host_itself() -> None:
    response = _response(200, {"router_url": "https://dash.example.com"})
    router_url, _ = _discover(response, base_url="https://dash.example.com")
    assert router_url == "https://dash.example.com"


def test_old_instance_is_none() -> None:
    assert _discover(_response(404))[0] is None


@pytest.mark.parametrize("status_code", [301, 500, 503])
def test_other_statuses_fail_rather_than_fall_back(status_code: int) -> None:
    with pytest.raises(DiscoveryError):
        _discover(_response(status_code))


@pytest.mark.parametrize(
    "body",
    [
        {"router_url": "https://evil.example.org"},
        {"router_url": "https://notexample.com"},
        {"router_url": "http://dash.example.com"},
        {"router_url": "https://dash.example.com/path"},
        {"router_url": 5},
        {},
        ValueError("not json"),
    ],
)
def test_rejects_unusable_answers(body: Any) -> None:
    with pytest.raises(DiscoveryError):
        _discover(_response(200, body))


def test_network_errors_fail() -> None:
    with patch("compute_space_cli.discovery.httpx.get", side_effect=httpx.ConnectError("down")):
        with pytest.raises(DiscoveryError):
            discover_router_url("https://example.com")


@pytest.mark.parametrize(
    ("discovered", "expected_url"), [("https://dash.a.com", "https://dash.a.com"), (None, "https://a.com")]
)
def test_api_commands_look_up_the_router_each_run_without_saving_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovered: str | None, expected_url: str
) -> None:
    monkeypatch.setattr(cli_config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(cli_config, "LEGACY_CONFIG_FILE", tmp_path / "legacy.toml")
    MultiConfig(instances={"a.com": Instance(hostname="a.com", token="t")}, default_instance="a.com").save()

    with patch("compute_space_cli.main.discover_router_url", return_value=discovered) as mock_discover:
        inst = resolve_api_instance(SimpleNamespace(instance=None))  # type: ignore[arg-type]

    mock_discover.assert_called_once_with("https://a.com")
    assert inst.url == expected_url
    assert MultiConfig.load().instances["a.com"].router_url is None
