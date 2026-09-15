#!/usr/bin/env python3
"""Report which pins in ``tests/external_pins.toml`` have fallen behind upstream.

Pinning stops the world moving under us; it does not stop the world moving.  This
resolves what each pin *tracks* (a git branch's head, an image tag's current
digest) and prints the ones that have drifted, so a bump is a deliberate,
reviewed act rather than something noticed when a build breaks.

Advisory by default: it exits 0 even when pins are stale, so the CI job wired to
it is a signal, not a gate.  Pass ``--strict`` to exit non-zero on drift.

    pixi run -e dev python scripts/check_pin_drift.py [--strict]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

# Run as a script from anywhere; the table lives next to the tests that use it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import external_pins  # noqa: E402

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def _split_registry(repo: str) -> tuple[str, str]:
    """Split ``repo`` into (registry host, path), applying Docker Hub's defaults.

    ``ghcr.io/astral-sh/uv`` -> ("ghcr.io", "astral-sh/uv")
    ``sigoden/dufs``         -> ("registry-1.docker.io", "sigoden/dufs")
    ``python``               -> ("registry-1.docker.io", "library/python")
    """
    head, _, rest = repo.partition("/")
    if "." in head or ":" in head:
        return head, rest
    return "registry-1.docker.io", repo if "/" in repo else f"library/{repo}"


def _registry_token(registry: str, path: str) -> str | None:
    realm = {
        "registry-1.docker.io": "https://auth.docker.io/token?service=registry.docker.io",
        "ghcr.io": "https://ghcr.io/token?service=ghcr.io",
    }.get(registry)
    if realm is None:
        return None
    with urllib.request.urlopen(f"{realm}&scope=repository:{path}:pull", timeout=20) as resp:
        return json.load(resp).get("token")


def current_image_digest(repo: str, tag: str) -> str:
    """Resolve what ``repo:tag`` points at right now."""
    registry, path = _split_registry(repo)
    headers = {"Accept": MANIFEST_ACCEPT}
    token = _registry_token(registry, path)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://{registry}/v2/{path}/manifests/{tag}", headers=headers, method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as resp:
        digest = resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError(f"{registry} returned no content digest for {repo}:{tag}")
    return digest


def current_git_head(url: str, branch: str) -> str:
    out = subprocess.run(["git", "ls-remote", url, branch], capture_output=True, text=True, check=True).stdout
    if not out.strip():
        raise RuntimeError(f"{url} has no branch {branch}")
    return out.split()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit non-zero when a pin has drifted")
    args = parser.parse_args()

    data = external_pins.load()
    stale: list[str] = []
    errors: list[str] = []

    for entry in data.get("git", []):
        label = f"git  {entry['name']}"
        try:
            head = current_git_head(entry["url"], entry["tracks"])
        except Exception as exc:  # noqa: BLE001 - advisory tool; report and continue
            errors.append(f"{label}: could not resolve {entry['tracks']}: {exc}")
            continue
        if head == entry["ref"]:
            print(f"ok      {label} @ {entry['ref'][:12]}")
        else:
            stale.append(f"{label}: pinned {entry['ref'][:12]}, {entry['tracks']} is now {head[:12]}")

    for entry in data.get("image", []):
        label = f"image {entry['name']}"
        try:
            digest = current_image_digest(entry["repo"], entry["tag"])
        except Exception as exc:  # noqa: BLE001 - advisory tool; report and continue
            errors.append(f"{label}: could not resolve {entry['repo']}:{entry['tag']}: {exc}")
            continue
        if digest == entry["digest"]:
            print(f"ok      {label} @ {entry['tag']}")
        else:
            stale.append(
                f"{label}: {entry['repo']}:{entry['tag']} was retagged ({entry['digest'][:19]} -> {digest[:19]})"
            )

    for message in errors:
        print(f"ERROR   {message}")
    for message in stale:
        print(f"STALE   {message}")

    if stale:
        print(
            f"\n{len(stale)} pin(s) behind upstream. Review the upstream changes, then bump tests/external_pins.toml."
        )
    else:
        print("\nAll pins current.")
    return 1 if (args.strict and (stale or errors)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
