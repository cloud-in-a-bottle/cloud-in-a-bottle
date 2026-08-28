"""OpenHost -> Cloud in a Bottle rename: the data-dir override env var is read
under the new BOTTLE_ name (preferred) and the legacy OPENHOST_ name, and writers
export both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openhost_system_agent.updater import paths


def test_prefers_bottle_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, "/legacy")
    monkeypatch.setenv(paths.DATA_DIR_ENV_NEW, "/bottle")
    assert paths.data_dir_env_value() == "/bottle"
    assert paths.data_dir() == Path("/bottle")


def test_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV_NEW, raising=False)
    monkeypatch.setenv(paths.DATA_DIR_ENV, "/legacy")
    assert paths.data_dir_env_value() == "/legacy"


def test_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV_NEW, raising=False)
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    assert paths.data_dir_env_value() is None
    # data_dir() falls back to the shared default rather than raising.
    assert str(paths.data_dir()).endswith("/persistent_data/openhost")


def test_setenv_pairs_covers_both_names() -> None:
    pairs = paths.data_dir_setenv_pairs("/some/dir")
    assert f"{paths.DATA_DIR_ENV_NEW}=/some/dir" in pairs
    assert f"{paths.DATA_DIR_ENV}=/some/dir" in pairs
