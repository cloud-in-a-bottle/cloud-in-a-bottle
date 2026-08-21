"""Unit tests for ``find_manifest_dir`` (walk-up manifest discovery)."""

from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.core.manifest import MANIFEST_FILENAMES
from openhost_test_harness.stack import find_manifest_dir

CANONICAL = MANIFEST_FILENAMES[0]
_MINIMAL = '[app]\nname = "x"\nversion = "0.1"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'


def test_finds_manifest_in_start_dir(tmp_path: Path) -> None:
    (tmp_path / CANONICAL).write_text(_MINIMAL)
    assert find_manifest_dir(tmp_path) == tmp_path.resolve()


def test_walks_up_to_ancestor(tmp_path: Path) -> None:
    (tmp_path / CANONICAL).write_text(_MINIMAL)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_manifest_dir(nested) == tmp_path.resolve()


def test_falls_back_to_legacy_openhost_toml(tmp_path: Path) -> None:
    (tmp_path / "openhost.toml").write_text(_MINIMAL)
    assert find_manifest_dir(tmp_path) == tmp_path.resolve()


def test_prefers_canonical_over_legacy(tmp_path: Path) -> None:
    (tmp_path / CANONICAL).write_text(_MINIMAL)
    (tmp_path / "openhost.toml").write_text(_MINIMAL)
    # Discovery is by directory, not filename, but the dir must still resolve.
    assert find_manifest_dir(tmp_path) == tmp_path.resolve()


def test_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / CANONICAL).write_text(_MINIMAL)
    monkeypatch.chdir(tmp_path)
    assert find_manifest_dir() == tmp_path.resolve()


def test_raises_when_no_manifest_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=CANONICAL):
        find_manifest_dir(tmp_path)
