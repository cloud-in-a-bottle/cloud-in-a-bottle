import asyncio
from pathlib import Path

import pytest

import compute_space.core.tls.util as tls_util


class _HungProcess:
    returncode: int | None = None

    def __init__(self) -> None:
        self.killed = False
        self.waited = False
        self.communicate_cancelled = False
        self.communicate_started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        try:
            return await asyncio.Future[tuple[bytes, bytes]]()
        except asyncio.CancelledError:
            self.communicate_cancelled = True
            raise

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        self.returncode = -9
        return self.returncode


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
async def test_dig_txt_kills_and_reaps_process_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _HungProcess()

    async def spawn(*args: object, **kwargs: object) -> _HungProcess:
        return proc

    monkeypatch.setattr(tls_util.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tls_util, "_DIG_TIMEOUT_SECONDS", 0.01)

    assert await tls_util._dig_txt("_acme-challenge.example.com", "8.8.8.8") == set()
    assert proc.communicate_cancelled is True
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_acquire_cert_clears_txt_records_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    calls: list[str] = []

    async def cancel_on_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    def clear_txt(path: Path) -> None:
        calls.append("clear")
        if cleanup_fails:
            raise OSError("zone file unavailable")

    monkeypatch.setattr(tls_util.client, "ClientNetwork", _FakeNetwork)
    monkeypatch.setattr(tls_util.client, "ClientV2", _FakeAcmeClient)
    monkeypatch.setattr(tls_util.messages.Directory, "from_json", staticmethod(lambda value: object()))
    monkeypatch.setattr(tls_util.challenges, "DNS01", _FakeDns01)
    monkeypatch.setattr(tls_util.dns_module, "append_txt_records", lambda path, records: calls.append("append"))
    monkeypatch.setattr(tls_util.dns_module, "clear_txt", clear_txt)
    monkeypatch.setattr(tls_util.asyncio, "sleep", cancel_on_sleep)

    with pytest.raises(asyncio.CancelledError):
        await tls_util._acquire_cert_dns01(
            domains=["example.com", "*.example.com"],
            directory_url="https://acme.test/directory",
            coredns_zonefile_path=tmp_path / "zonefile",
            account_key=object(),  # type: ignore[arg-type]
        )

    assert calls == ["append", "clear"]


@pytest.mark.asyncio
async def test_dig_txt_reaps_process_and_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _HungProcess()

    async def spawn(*args: object, **kwargs: object) -> _HungProcess:
        return proc

    monkeypatch.setattr(tls_util.asyncio, "create_subprocess_exec", spawn)

    task = asyncio.create_task(tls_util._dig_txt("_acme-challenge.example.com", "8.8.8.8"))
    await proc.communicate_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.communicate_cancelled is True
    assert proc.killed is True
    assert proc.waited is True
