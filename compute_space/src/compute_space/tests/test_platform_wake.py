import datetime
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from compute_space.config import DefaultConfig
from compute_space.core.auto_update.config import AutoUpdateConfig
from compute_space.core.auto_update.config import write_auto_update_config
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.core.platform_wake import client as client_mod
from compute_space.core.platform_wake.client import WakeApi
from compute_space.core.platform_wake.client import hold_awake
from compute_space.core.platform_wake.schedule import next_wake_at
from compute_space.core.tls.renewal import RENEW_BEFORE
from compute_space.db import init_db
from compute_space.tests.conftest import open_db
from compute_space.tests.test_cert_renewal import _write_self_signed_cert

_NOW = datetime.datetime.now(datetime.UTC)


def _config(tmp_path: Path, *domains: Domain) -> DefaultConfig:
    config = DefaultConfig(data_root_dir=str(tmp_path))
    config.openhost_data_path.mkdir(parents=True)
    config.certs_dir.mkdir()
    init_db(config.db_path)
    with closing(open_db(config)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls) for d in domains[1:]])
    return config


def _cert(config: DefaultConfig, name: str, expires_in: datetime.timedelta) -> None:
    cert_path, key_path = config.cert_key_paths_for(name)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    _write_self_signed_cert(cert_path, key_path, _NOW + expires_in)


def _set_update(config: DefaultConfig, enabled: bool) -> None:
    with closing(open_db(config)) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=enabled, time_utc=datetime.time(4, 0)))


def test_next_wake_is_the_earliest_cert_renewal(tmp_path: Path) -> None:
    config = _config(tmp_path, Domain("a.example.com", tls=True), Domain("b.example.com", tls=True))
    _set_update(config, enabled=False)
    _cert(config, "a.example.com", datetime.timedelta(days=60))
    _cert(config, "b.example.com", datetime.timedelta(days=30))
    with closing(open_db(config)) as db:
        wake_at = next_wake_at(config, db, _NOW)
    assert wake_at is not None
    assert abs(wake_at - (_NOW + datetime.timedelta(days=30) - RENEW_BEFORE)) < datetime.timedelta(seconds=1)


def test_next_wake_includes_the_daily_update(tmp_path: Path) -> None:
    config = _config(tmp_path, Domain("a.example.com", tls=True))
    _cert(config, "a.example.com", datetime.timedelta(days=60))
    _set_update(config, enabled=True)
    with closing(open_db(config)) as db:
        wake_at = next_wake_at(config, db, _NOW)
    assert wake_at is not None and wake_at - _NOW <= datetime.timedelta(days=1)
    assert (wake_at.hour, wake_at.minute) == (4, 0)


def test_nothing_scheduled(tmp_path: Path) -> None:
    config = _config(tmp_path, Domain("lvh.me:8080", tls=False))
    _set_update(config, enabled=False)
    # A cert already inside its renewal window is being renewed now, not later.
    _cert(config, "lvh.me", RENEW_BEFORE - datetime.timedelta(days=1))
    with closing(open_db(config)) as db:
        assert next_wake_at(config, db, _NOW) is None


def test_load(tmp_path: Path) -> None:
    assert WakeApi.load(tmp_path / "missing.toml") is None
    path = tmp_path / "platform.toml"
    path.write_text('[wake]\nurl = "http://203.0.113.7:8101/v1/guest/"\ntoken = "t0k"\n')
    assert WakeApi.load(path) == WakeApi(url="http://203.0.113.7:8101/v1/guest", token="t0k")


@pytest.mark.asyncio
async def test_hold_awake_without_a_platform_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "PLATFORM_FILE", tmp_path / "missing.toml")
    await hold_awake(datetime.timedelta(minutes=5))


@pytest.mark.asyncio
async def test_hold_awake_swallows_platform_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "platform.toml"
    path.write_text('[wake]\nurl = "http://127.0.0.1:9/v1/guest"\ntoken = "t0k"\n')
    monkeypatch.setattr(client_mod, "PLATFORM_FILE", path)
    sent: list[httpx.Request] = []

    async def fail(self: WakeApi, method: str, path: str, body: dict[str, float | None]) -> None:
        sent.append(httpx.Request(method, f"{self.url}{path}", json=body))
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(WakeApi, "_send", fail)
    await hold_awake(datetime.timedelta(minutes=5))
    assert [(r.method, str(r.url)) for r in sent] == [("POST", "http://127.0.0.1:9/v1/guest/busy")]
