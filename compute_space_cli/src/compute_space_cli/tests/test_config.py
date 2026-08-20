"""Unit tests for config module — no network required."""

from pathlib import Path

import pytest
import tomli_w

from compute_space_cli import config as cli_config
from compute_space_cli.config import ConfigFileNotFoundError
from compute_space_cli.config import ConfigInvalidError
from compute_space_cli.config import Instance
from compute_space_cli.config import InstanceNotFoundError
from compute_space_cli.config import MultiConfig
from compute_space_cli.config import normalize_url


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _inst(hostname: str = "a.com", token: str = "t", alias: str | None = None) -> Instance:
    return Instance(hostname=hostname, token=token, alias=alias)


def _make_multi(
    instances: dict[str, Instance] | None = None,
    default: str | None = None,
) -> MultiConfig:
    return MultiConfig(instances=instances or {}, default_instance=default)


class TestInstance:
    def test_fields(self) -> None:
        inst = _inst("example.com", "tok", "ex")
        assert inst.hostname == "example.com"
        assert inst.token == "tok"
        assert inst.alias == "ex"

    def test_url_property(self) -> None:
        assert _inst("example.com").url == "https://example.com"

    def test_alias_defaults_none(self) -> None:
        assert _inst("a.com").alias is None

    def test_frozen(self) -> None:
        inst = _inst()
        with pytest.raises(AttributeError):
            inst.hostname = "b.com"  # type: ignore[misc]


class TestNormalizeUrl:
    def test_bare_hostname(self) -> None:
        assert normalize_url("example.com") == "https://example.com"

    def test_https_passthrough(self) -> None:
        assert normalize_url("https://example.com") == "https://example.com"

    def test_http_passthrough(self) -> None:
        assert normalize_url("http://localhost:8080") == "http://localhost:8080"


class TestMultiConfigSaveLoad:
    def test_roundtrip(self, tmp_config: Path) -> None:
        original = _make_multi(
            instances={
                "a.com": _inst("a.com", "tok-a", alias="a"),
                "b.com": _inst("b.com", "tok-b"),
            },
            default="a.com",
        )
        original.save(tmp_config)
        loaded = MultiConfig.load(tmp_config)
        assert loaded.default_instance == "a.com"
        assert len(loaded.instances) == 2
        assert loaded.instances["a.com"].url == "https://a.com"
        assert loaded.instances["a.com"].alias == "a"
        assert loaded.instances["b.com"].token == "tok-b"
        assert loaded.instances["b.com"].alias is None

    def test_load_legacy_format(self, tmp_config: Path) -> None:
        with open(tmp_config, "wb") as f:
            tomli_w.dump({"url": "https://old.com", "token": "old-tok"}, f)
        loaded = MultiConfig.load(tmp_config)
        assert loaded.default_instance == "old.com"
        assert loaded.instances["old.com"].url == "https://old.com"
        assert loaded.instances["old.com"].token == "old-tok"

    def test_load_missing_file(self, tmp_config: Path) -> None:
        with pytest.raises(ConfigFileNotFoundError, match="not found"):
            MultiConfig.load(tmp_config)

    def test_load_empty_file(self, tmp_config: Path) -> None:
        tmp_config.write_bytes(b"")
        loaded = MultiConfig.load(tmp_config)
        assert len(loaded.instances) == 0
        assert loaded.default_instance is None

    def test_load_malformed_instance(self, tmp_config: Path) -> None:
        with open(tmp_config, "wb") as f:
            tomli_w.dump({"instances": {"bad": {"url": 123}}}, f)
        with pytest.raises(ConfigInvalidError, match="malformed"):
            MultiConfig.load(tmp_config)

    def test_load_invalid_toml_syntax(self, tmp_config: Path) -> None:
        tmp_config.write_bytes(b"[invalid toml {{{")
        with pytest.raises(ConfigInvalidError, match="invalid TOML syntax"):
            MultiConfig.load(tmp_config)

    def test_load_instances_not_a_table(self, tmp_config: Path) -> None:
        with open(tmp_config, "wb") as f:
            tomli_w.dump({"instances": "not-a-table"}, f)
        with pytest.raises(ConfigInvalidError, match="malformed"):
            MultiConfig.load(tmp_config)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "config.toml"
        _make_multi(instances={"x.com": _inst("x.com")}, default="x.com").save(path)
        loaded = MultiConfig.load(path)
        assert loaded.instances["x.com"].hostname == "x.com"


class TestLegacyConfigFallback:
    """Default-path resolution prefers ``~/.bottle`` and falls back to ``~/.openhost``.

    When ``load()`` is called with no explicit path it must prefer the ``~/.bottle``
    config file but transparently read the legacy ``~/.openhost`` file when the
    former is absent, then migrate forward to ``~/.bottle`` on the next save.
    """

    @pytest.fixture
    def paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        new = tmp_path / "bottle" / "compute_space_cli.toml"
        legacy = tmp_path / "openhost" / "compute_space_cli.toml"
        monkeypatch.setattr(cli_config, "CONFIG_FILE", new)
        monkeypatch.setattr(cli_config, "LEGACY_CONFIG_FILE", legacy)
        return new, legacy

    def _write(self, path: Path, hostname: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump({"instances": {hostname: {"token": "tok", "alias": "al"}}, "default_instance": hostname}, f)

    def _write_flat_legacy(self, path: Path, url: str) -> None:
        """Write the pre-multi-instance flat format (top-level url/token)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump({"url": url, "token": "flat-tok"}, f)

    def test_prefers_new_path(self, paths: tuple[Path, Path]) -> None:
        new, legacy = paths
        self._write(new, "new.com")
        self._write(legacy, "legacy.com")
        assert MultiConfig.load().default_instance == "new.com"

    def test_falls_back_to_legacy(self, paths: tuple[Path, Path]) -> None:
        new, legacy = paths
        self._write(legacy, "legacy.com")
        assert not new.exists()
        assert MultiConfig.load().default_instance == "legacy.com"

    def test_falls_back_to_legacy_flat_format(self, paths: tuple[Path, Path]) -> None:
        """The real migration case: an original ~/.openhost in the pre-multi-instance
        flat (url/token) format must load via the default-path fallback."""
        new, legacy = paths
        self._write_flat_legacy(legacy, "https://old.com")
        assert not new.exists()
        loaded = MultiConfig.load()
        assert loaded.default_instance == "old.com"
        assert loaded.instances["old.com"].token == "flat-tok"

    def test_missing_both_raises_pointing_at_new(self, paths: tuple[Path, Path]) -> None:
        new, _legacy = paths
        with pytest.raises(ConfigFileNotFoundError, match=str(new)):
            MultiConfig.load()

    def test_save_migrates_forward_to_new_path(self, paths: tuple[Path, Path]) -> None:
        """Loading a legacy config then saving writes to the new ~/.bottle path,
        preserves instance fields, and leaves the legacy file untouched."""
        new, legacy = paths
        self._write(legacy, "legacy.com")
        legacy_bytes_before = legacy.read_bytes()

        loaded = MultiConfig.load()
        loaded.save()

        assert new.exists()
        migrated = MultiConfig.load(new)
        assert migrated.default_instance == "legacy.com"
        # Instance fields survive the migration round-trip.
        assert migrated.instances["legacy.com"].token == "tok"
        assert migrated.instances["legacy.com"].alias == "al"
        # save() must not touch the legacy file.
        assert legacy.read_bytes() == legacy_bytes_before


class TestMultiConfigResolve:
    def test_explicit_name(self) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        assert multi.resolve(instance_name="b.com").url == "https://b.com"

    def test_resolve_by_alias(self) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com", alias="dev")},
            default="a.com",
        )
        assert multi.resolve(instance_name="dev").hostname == "a.com"

    def test_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        monkeypatch.setenv("BOTTLE_INSTANCE", "b.com")
        assert multi.resolve().url == "https://b.com"

    def test_default_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOTTLE_INSTANCE", raising=False)
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        assert multi.resolve().url == "https://a.com"

    def test_no_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOTTLE_INSTANCE", raising=False)
        multi = _make_multi(instances={"only.com": _inst("only.com")})
        with pytest.raises(InstanceNotFoundError, match="No default instance set"):
            multi.resolve()

    def test_nonexistent_name_raises(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")}, default="a.com")
        with pytest.raises(InstanceNotFoundError, match="not found"):
            multi.resolve(instance_name="nope")

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        monkeypatch.setenv("BOTTLE_INSTANCE", "a.com")
        assert multi.resolve(instance_name="b.com").url == "https://b.com"

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        monkeypatch.setenv("BOTTLE_INSTANCE", "b.com")
        assert multi.resolve().url == "https://b.com"

    def test_legacy_oh_instance_env_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pre-rename OH_INSTANCE env var is intentionally no longer honored.

        Only BOTTLE_INSTANCE is read; a stale OH_INSTANCE must not silently select an
        instance, so resolution falls through to the configured default.
        """
        monkeypatch.delenv("BOTTLE_INSTANCE", raising=False)
        monkeypatch.setenv("OH_INSTANCE", "b.com")
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        assert multi.resolve().url == "https://a.com"


class TestUpsertInstance:
    def test_add_to_empty_no_default(self) -> None:
        result = _make_multi().upsert_instance(_inst("a.com"))
        assert "a.com" in result.instances
        assert result.default_instance is None

    def test_set_default_true(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")}, default="a.com")
        result = multi.upsert_instance(_inst("b.com"), set_default=True)
        assert result.default_instance == "b.com"

    def test_preserves_existing_default(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")}, default="a.com")
        result = multi.upsert_instance(_inst("b.com"))
        assert result.default_instance == "a.com"
        assert "b.com" in result.instances

    def test_replaces_existing_instance(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com", "old")}, default="a.com")
        result = multi.upsert_instance(_inst("a.com", "new"))
        assert result.instances["a.com"].token == "new"

    def test_original_unchanged(self) -> None:
        multi = _make_multi()
        multi.upsert_instance(_inst("a.com"))
        assert "a.com" not in multi.instances


class TestRemoveInstance:
    def test_remove_non_default(self) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        result = multi.remove_instance("b.com")
        assert "b.com" not in result.instances
        assert result.default_instance == "a.com"

    def test_remove_default_clears(self) -> None:
        multi = _make_multi(
            instances={"a.com": _inst("a.com"), "b.com": _inst("b.com")},
            default="a.com",
        )
        result = multi.remove_instance("a.com")
        assert "a.com" not in result.instances
        assert result.default_instance is None

    def test_remove_by_alias(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com", alias="dev")})
        result = multi.remove_instance("dev")
        assert "a.com" not in result.instances

    def test_remove_last_instance(self) -> None:
        multi = _make_multi(instances={"only.com": _inst("only.com")}, default="only.com")
        result = multi.remove_instance("only.com")
        assert len(result.instances) == 0
        assert result.default_instance is None

    def test_remove_nonexistent_raises(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")})
        with pytest.raises(InstanceNotFoundError, match="nope"):
            multi.remove_instance("nope")

    def test_original_unchanged(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")}, default="a.com")
        multi.remove_instance("a.com")
        assert "a.com" in multi.instances


class TestGetInstance:
    def test_by_hostname(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")})
        assert multi.get_instance("a.com").hostname == "a.com"

    def test_by_alias(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com", alias="dev")})
        assert multi.get_instance("dev").hostname == "a.com"

    def test_not_found(self) -> None:
        multi = _make_multi(instances={"a.com": _inst("a.com")})
        with pytest.raises(InstanceNotFoundError, match="nope"):
            multi.get_instance("nope")
