import os
import tomllib
from pathlib import Path
from typing import Any
from typing import Self

import attr
import tomli_w

CONFIG_DIR = Path.home() / ".cb"
CONFIG_FILE = CONFIG_DIR / "compute_space_cli.toml"

# Legacy location used before the CLI was renamed oh -> cb. Still read as a
# fallback so existing logins keep working; the config migrates forward to
# CONFIG_FILE on the next save.
LEGACY_CONFIG_DIR = Path.home() / ".openhost"
LEGACY_CONFIG_FILE = LEGACY_CONFIG_DIR / "compute_space_cli.toml"


class ConfigFileNotFoundError(Exception):
    pass


class ConfigInvalidError(Exception):
    pass


class InstanceNotFoundError(Exception):
    pass


def normalize_url(url: str) -> str:
    """Ensure a URL has a protocol prefix."""
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def hostname_from_url(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").rstrip("/")


@attr.s(auto_attribs=True, frozen=True)
class Instance:
    """A single Cloud in a Bottle compute-space instance."""

    hostname: str = attr.ib()
    token: str = attr.ib()
    alias: str | None = attr.ib(default=None)
    ssh_key: str | None = attr.ib(default=None)

    @property
    def url(self) -> str:
        return f"https://{self.hostname}"


@attr.s(auto_attribs=True, frozen=True)
class MultiConfig:
    """Top-level configuration supporting multiple named instances."""

    instances: dict[str, Instance] = attr.ib(factory=dict)
    default_instance: str | None = attr.ib(default=None)

    def evolve(self, **changes: Any) -> Self:  # noqa: ANN401
        """Return a copy with the given fields replaced."""
        return attr.evolve(self, **changes)

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        raw: dict[str, object] = {}
        if self.default_instance:
            raw["default_instance"] = self.default_instance
        instances_raw: dict[str, object] = {}
        for hostname, inst in self.instances.items():
            entry: dict[str, object] = {"token": inst.token}
            if inst.alias:
                entry["alias"] = inst.alias
            if inst.ssh_key:
                entry["ssh_key"] = inst.ssh_key
            instances_raw[hostname] = entry
        if instances_raw:
            raw["instances"] = instances_raw
        with open(path, "wb") as f:
            tomli_w.dump(raw, f)

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        # When no explicit path is given, prefer the current config file but
        # fall back to the legacy ~/.openhost location so users who logged in
        # before the oh -> cb rename keep working (the next save migrates it).
        if path is None:
            if CONFIG_FILE.exists():
                path = CONFIG_FILE
            elif LEGACY_CONFIG_FILE.exists():
                path = LEGACY_CONFIG_FILE
            else:
                path = CONFIG_FILE
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            raise ConfigFileNotFoundError(f"Config file not found at {path}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigInvalidError(f"Config file at {path} has invalid TOML syntax: {e}") from None

        try:
            if "instances" in data:
                instances: dict[str, Instance] = {}
                for hostname, raw_inst in data["instances"].items():
                    if not isinstance(raw_inst, dict):
                        raise TypeError(f"Instance '{hostname}' must be a table")
                    instances[hostname] = Instance(
                        hostname=hostname,
                        token=raw_inst["token"],
                        alias=raw_inst.get("alias"),
                        ssh_key=raw_inst.get("ssh_key"),
                    )
                raw_default = data.get("default_instance")
                if raw_default is not None and not isinstance(raw_default, str):
                    raise TypeError(f"default_instance must be a string, got {type(raw_default).__name__}")
                return cls(instances=instances, default_instance=raw_default)

            # Legacy format: bare url + token at top level.
            if "url" in data and "token" in data:
                hostname = hostname_from_url(data["url"])
                inst = Instance(hostname=hostname, token=data["token"])
                return cls(instances={hostname: inst}, default_instance=hostname)
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            raise ConfigInvalidError(f"Config file at {path} is malformed: {e}") from None

        return cls()

    def upsert_instance(self, inst: Instance, *, set_default: bool = False) -> Self:
        """Return a new config with the given instance added or replaced."""
        instances = dict(self.instances)
        instances[inst.hostname] = inst
        default = inst.hostname if set_default else self.default_instance
        return self.evolve(instances=instances, default_instance=default)

    def remove_instance(self, name: str) -> Self:
        """Return a new config with the named instance removed."""
        hostname = self._resolve_name(name)
        instances = {k: v for k, v in self.instances.items() if k != hostname}
        default = self.default_instance
        if default == hostname:
            default = None
        return self.evolve(instances=instances, default_instance=default)

    def _resolve_name(self, name: str) -> str:
        """Resolve a name (hostname or alias) to a hostname. Raises if not found."""
        if name in self.instances:
            return name
        for hostname, inst in self.instances.items():
            if inst.alias == name:
                return hostname
        raise InstanceNotFoundError(
            f"Instance '{name}' not found. Run 'cb instance list' to see configured instances."
        )

    def get_instance(self, name: str) -> Instance:
        """Return an instance by hostname or alias."""
        return self.instances[self._resolve_name(name)]

    def resolve(self, instance_name: str | None = None) -> Instance:
        """Resolve which instance to use.

        Priority: explicit name > CB_INSTANCE env var > default_instance.
        """
        name = instance_name
        if not name:
            name = os.environ.get("CB_INSTANCE")
        if not name:
            name = self.default_instance

        if not name:
            if not self.instances:
                raise InstanceNotFoundError("No instances configured. Run 'cb instance login' first.")
            raise InstanceNotFoundError(
                "No default instance set. Use --instance <name>, or set a default with:\n"
                "  cb instance set-default <name>\n"
                "Run 'cb instance list' to see configured instances."
            )

        return self.get_instance(name)


def get_multi_config() -> MultiConfig:
    return MultiConfig.load()
