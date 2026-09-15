"""Reader for ``tests/external_pins.toml``, the pinned-external-dependency table.

Two consumers share this module: the offline guard (``test_external_pins.py``)
and the online drift reporter (``scripts/check_pin_drift.py``).  Tests that need
a pinned dependency ask for it by name rather than writing the sha inline, so
there is exactly one place a pin is stated.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

PINS_PATH = Path(__file__).resolve().parent / "external_pins.toml"
REPO_ROOT = PINS_PATH.parent.parent


def load() -> dict[str, list[dict[str, Any]]]:
    with PINS_PATH.open("rb") as f:
        return tomllib.load(f)


def _by_name(kind: str) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in load().get(kind, [])}


def git_url(name: str) -> str:
    """Return ``<url>@<commit>`` for a pinned repo, the form add_app understands."""
    entry = _by_name("git")[name]
    return f"{entry['url']}@{entry['ref']}"


def image_ref(name: str) -> str:
    """Return ``<repo>:<tag>@<digest>`` for a pinned image."""
    entry = _by_name("image")[name]
    return f"{entry['repo']}:{entry['tag']}@{entry['digest']}"


def all_image_refs() -> set[str]:
    return {image_ref(entry["name"]) for entry in load().get("image", [])}


def all_git_urls() -> set[str]:
    return {git_url(entry["name"]) for entry in load().get("git", [])}
