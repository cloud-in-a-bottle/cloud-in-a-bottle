"""Guard: nothing in the tree reaches the network at an unpinned identity.

This is what keeps ``external_pins.toml`` authoritative rather than decorative.
It is offline and fast (it reads files, it does not resolve anything), so it runs
in the default test job: adding an unpinned ``FROM`` or an unpinned app repo URL
to a test fails here rather than a week later in e2e.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests import external_pins

REPO_ROOT = external_pins.REPO_ROOT

# `FROM <ref> [AS <stage>]` and `COPY --from=<ref>`.  Both can name either an
# image or an earlier build stage in the same file; only the former needs a pin.
FROM_RE = re.compile(r"^\s*FROM\s+(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$", re.IGNORECASE | re.MULTILINE)
COPY_FROM_RE = re.compile(r"--from=(?P<ref>\S+)")
# A GitHub repo URL, optionally with the `@<ref>` pin suffix add_app accepts.
GITHUB_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+(?:@[0-9a-f]{40})?")


def _dockerfiles() -> list[Path]:
    """Every tracked Dockerfile.  Asking git rather than walking the tree keeps
    vendored copies under .pixi/ and friends out of scope."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "*Dockerfile*"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(REPO_ROOT / rel for rel in out.split("\0") if rel)


@pytest.mark.parametrize("dockerfile", _dockerfiles(), ids=lambda p: str(p.relative_to(external_pins.REPO_ROOT)))
def test_dockerfiles_do_not_track_a_floating_tag(dockerfile: Path) -> None:
    """No Dockerfile may pull `:latest`, or a tag with no version at all.

    An already-versioned tag (`python:3.12-alpine`, `ubuntu:24.04`) is fine: it
    moves only for patches within that release.  `:latest` is the problem, since
    it walks across major versions without warning, which is how the file-browser
    image changed under the archive tests.  Pin those to a digest and give them a
    row in external_pins.toml.
    """
    text = dockerfile.read_text()
    stages: set[str] = set()
    refs: list[str] = []
    for match in FROM_RE.finditer(text):
        refs.append(match.group("ref"))
        if match.group("stage"):
            stages.add(match.group("stage"))
    refs.extend(m.group("ref") for m in COPY_FROM_RE.finditer(text))

    floating = [
        ref
        for ref in refs
        if ref not in stages and "@sha256:" not in ref and (ref.endswith(":latest") or ":" not in ref)
    ]
    assert not floating, (
        f"{dockerfile.relative_to(REPO_ROOT)} tracks floating tag(s) {floating}. "
        f"Pin to a digest and add a row to tests/external_pins.toml."
    )


@pytest.mark.parametrize(
    "test_file",
    sorted(p for p in (REPO_ROOT / "tests").glob("*.py")),
    ids=lambda p: p.name,
)
def test_repo_urls_in_tests_are_pinned(test_file: Path) -> None:
    """Every GitHub repo a test deploys must be pinned to a commit in the table.

    Scoped to the test suite: ``config.py``'s ``default_apps`` deliberately track
    their default branches, because that is what a real instance gets.
    """
    pinned = external_pins.all_git_urls()
    found = set(GITHUB_URL_RE.findall(test_file.read_text()))
    unpinned = sorted(url for url in found if url not in pinned)
    assert not unpinned, (
        f"{test_file.name} references unpinned repo(s) {unpinned}. "
        f"Add a row to tests/external_pins.toml and use `external_pins.git_url(<name>)`."
    )


def test_every_pin_is_actually_used() -> None:
    """A pin nobody consumes is a stale pin.  Each row's ``used_by`` files must
    exist and must really reference that pin.

    Tests name a git pin (``external_pins.git_url("bottled-minio")``) so the sha
    lives only in the table; a Dockerfile cannot call Python, so it carries the
    rendered ``repo:tag@digest`` and the table checks the two agree.
    """
    data = external_pins.load()
    expected = [(entry, entry["name"]) for entry in data.get("git", [])]
    expected += [(entry, external_pins.image_ref(entry["name"])) for entry in data.get("image", [])]
    for entry, needle in expected:
        for rel in entry["used_by"]:
            path = REPO_ROOT / rel
            assert path.exists(), f"pin {entry['name']} lists a missing used_by path: {rel}"
            assert needle in path.read_text(), f"pin {entry['name']} is not used by {rel} (expected {needle!r})"
