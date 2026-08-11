from __future__ import annotations

import socket

import pytest

from compute_space.core import util


def _no_default_route(*args: object, **kwargs: object) -> object:
    raise OSError("network is unreachable")


class _Sock:
    def __init__(self, sockname: str) -> None:
        self._sockname = sockname

    def __enter__(self) -> _Sock:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        return (self._sockname, 0)


def test_primary_probe_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "_lan_ip_from_host", lambda family: "192.168.1.99")
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock("10.0.0.5"))
    assert util.default_route_source_ip() == "10.0.0.5"


def test_fallback_when_no_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _no_default_route)
    monkeypatch.setattr(util, "_lan_ip_from_host", lambda family: "192.168.1.42")
    assert util.default_route_source_ip() == "192.168.1.42"


def test_fallback_picks_private_over_loopback_and_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _no_default_route)

    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, 0, 0, "", ("127.0.1.1", 0)),
            (socket.AF_INET, 0, 0, "", ("169.254.10.10", 0)),
            (socket.AF_INET, 0, 0, "", ("192.168.5.6", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.default_route_source_ip() == "192.168.5.6"


def test_fallback_link_local_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _no_default_route)

    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, 0, 0, "", ("169.254.10.10", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.default_route_source_ip() == "169.254.10.10"


def test_none_when_only_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _no_default_route)

    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, 0, 0, "", ("127.0.1.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.default_route_source_ip() is None


def test_primary_loopback_falls_through_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock("127.0.0.1"))
    monkeypatch.setattr(util, "_lan_ip_from_host", lambda family: "10.1.2.3")
    assert util.default_route_source_ip() == "10.1.2.3"
