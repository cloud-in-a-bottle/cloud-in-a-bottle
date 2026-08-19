"""Tests for the /api/compute_space_logs tail behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.exceptions import ServiceUnavailableException

import compute_space.web.routes.api.system as system_api


def test_logs_smaller_than_tail_served_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = tmp_path / "cs.log"
    log.write_text("first line\nsecond line\n")
    monkeypatch.setattr(system_api, "get_log_path", lambda: log)

    resp = system_api.compute_space_logs.fn()

    assert resp.status_code == 200
    assert resp.content == "first line\nsecond line\n"


def test_logs_larger_than_tail_served_from_line_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = tmp_path / "cs.log"
    line = "x" * 99 + "\n"
    log.write_text(line * 4000)  # 400 KB, above the 256 KiB tail
    monkeypatch.setattr(system_api, "get_log_path", lambda: log)

    resp = system_api.compute_space_logs.fn()

    assert resp.status_code == 200
    assert len(resp.content) <= system_api._LOG_TAIL_BYTES
    # The tail starts at a line boundary, not mid-line.
    assert resp.content.startswith("x" * 99 + "\n")
    assert resp.content.endswith(line)


def test_unconfigured_log_path_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_api, "get_log_path", lambda: None)

    with pytest.raises(ServiceUnavailableException) as excinfo:
        system_api.compute_space_logs.fn()

    # 503 keeps `detail` unmasked, unlike the 500 litestar would rewrite.
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "Log file not configured"
