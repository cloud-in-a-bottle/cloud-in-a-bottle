import asyncio

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
async def test_acquire_cert_clears_txt_records_on_cancellation(
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    calls: list[str] = []

    async def cancel_on_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    def clear(dns: object) -> None:
        calls.append("clear")
        if cleanup_fails:
            raise OSError("zone file unavailable")

    monkeypatch.setattr(tls_util.client, "ClientNetwork", _FakeNetwork)
    monkeypatch.setattr(tls_util.client, "ClientV2", _FakeAcmeClient)
    monkeypatch.setattr(tls_util.messages.Directory, "from_json", staticmethod(lambda value: object()))
    monkeypatch.setattr(tls_util.challenges, "DNS01", _FakeDns01)
    monkeypatch.setattr(tls_util.challenge, "publish", lambda dns, values: calls.append("publish"))
    monkeypatch.setattr(tls_util.challenge, "clear", clear)
    monkeypatch.setattr(tls_util.asyncio, "sleep", cancel_on_sleep)

    with pytest.raises(asyncio.CancelledError):
        await tls_util._acquire_cert_dns01(
            domains=["example.com", "*.example.com"],
            directory_url="https://acme.test/directory",
            dns=None,  # type: ignore[arg-type]  # unused once publish/clear are patched
            account_key=object(),  # type: ignore[arg-type]
        )

    assert calls == ["publish", "clear"]
