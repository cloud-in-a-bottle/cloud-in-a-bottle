import ssl

import pytest

from compute_space.core.proxy_target import LocalPort
from compute_space.core.proxy_target import client_for


@pytest.mark.asyncio
async def test_loopback_proxy_client_does_not_load_certificate_authorities(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> ssl.SSLContext:
        raise AssertionError("plain HTTP loopback must not load root certificate authorities")

    monkeypatch.setattr(ssl, "create_default_context", fail_if_called)
    client, base_url = client_for(LocalPort(9000), timeout=5)
    try:
        assert base_url == "http://127.0.0.1:9000"
    finally:
        await client.aclose()
