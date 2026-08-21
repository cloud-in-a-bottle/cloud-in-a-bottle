from __future__ import annotations

import socket

import pytest

from compute_space.core import util


def test_private_ip_prefers_rfc1918_over_loopback_and_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, 0, 0, "", ("127.0.1.1", 0)),
            (socket.AF_INET, 0, 0, "", ("169.254.10.10", 0)),
            (socket.AF_INET, 0, 0, "", ("192.168.5.6", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.private_ip() == "192.168.5.6"


def test_private_ip_link_local_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, 0, 0, "", ("169.254.10.10", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.private_ip() == "169.254.10.10"


def test_private_ip_none_when_only_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, 0, 0, "", ("127.0.1.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.private_ip() is None


def test_private_ip_none_when_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(host: str, *a: object, **k: object) -> object:
        raise OSError("no addresses")

    monkeypatch.setattr(socket, "getaddrinfo", raise_os_error)
    assert util.private_ip() is None


def test_private_ip6_skips_link_local_takes_ula(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET6, 0, 0, "", ("fe80::1%en0", 0)),
            (socket.AF_INET6, 0, 0, "", ("fd00::5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.private_ip6() == "fd00::5"


def test_private_ip6_none_when_only_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *a: object, **k: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET6, 0, 0, "", ("fe80::1%en0", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert util.private_ip6() is None


def test_is_bindable_only_for_addresses_on_this_host() -> None:
    assert util.is_bindable("127.0.0.1")
    assert util.is_bindable("::1")
    # TEST-NET-3 — reserved for documentation, so it is never configured on a real interface.
    assert not util.is_bindable("203.0.113.10")
