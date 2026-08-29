"""DNS-01 acquisition: that challenge records come back down however the attempt ends."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import compute_space.core.tls.util as tls_util


class _JsonResponse:
    def json(self) -> dict[str, str]:
        return {}


class _FakeNetwork:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.account: object | None = None

    def get(self, url: str) -> _JsonResponse:
        return _JsonResponse()


class _FakeDns01:
    pass


class _FakeChallengeBody:
    chall = _FakeDns01()
    status = tls_util.messages.STATUS_PENDING

    def validation(self, account_key: object) -> str:
        return "validation-value"


class _AuthorizationBody:
    identifier = "example.com"
    status = tls_util.messages.STATUS_PENDING
    challenges = [_FakeChallengeBody()]


class _Authorization:
    body = _AuthorizationBody()


class _OrderBody:
    status = tls_util.messages.STATUS_PENDING


class _Order:
    body = _OrderBody()
    authorizations = [_Authorization()]
    fullchain_pem: str | None = None


class _Account:
    uri = "https://acme.test/account/1"


class _FakeAcmeClient:
    def __init__(self, directory: object, net: _FakeNetwork) -> None:
        self.net = net

    def new_account(self, registration: object) -> object:
        return object()

    def query_registration(self, registration: object) -> _Account:
        return _Account()

    def new_order(self, csr: bytes) -> _Order:
        return _Order()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_acquire_cert_clears_challenge_records_on_cancellation(
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    # Cancellation lands between publishing the tokens and finalizing the order — the window a
    # plain "clean up on the way out" would leave the zone holding live tokens forever. A failing
    # cleanup must not replace the cancellation the caller is waiting on, either.
    calls: list[str] = []

    async def publish(dns: object, values: list[str]) -> None:
        calls.append("publish")

    async def wait_until_visible(dns: object, domain: str, values: list[str]) -> None:
        raise asyncio.CancelledError

    async def clear(dns: object) -> None:
        calls.append("clear")
        if cleanup_fails:
            raise RuntimeError("DNS service unavailable")

    monkeypatch.setattr(tls_util.client, "ClientNetwork", _FakeNetwork)
    monkeypatch.setattr(tls_util.client, "ClientV2", _FakeAcmeClient)
    monkeypatch.setattr(tls_util.messages.Directory, "from_json", staticmethod(lambda value: object()))
    monkeypatch.setattr(tls_util.challenges, "DNS01", _FakeDns01)
    monkeypatch.setattr(tls_util.challenge, "publish", publish)
    monkeypatch.setattr(tls_util.challenge, "wait_until_visible", wait_until_visible)
    monkeypatch.setattr(tls_util.challenge, "clear", clear)

    with pytest.raises(asyncio.CancelledError):
        await tls_util._acquire_cert_dns01(
            domains=["example.com", "*.example.com"],
            directory_url="https://acme.test/directory",
            dns=cast_any(object()),
            account_key=cast_any(object()),
        )

    assert calls == ["publish", "clear"]


def cast_any(value: object) -> Any:
    """The fakes stand in for typed collaborators the code only calls through."""
    return value
