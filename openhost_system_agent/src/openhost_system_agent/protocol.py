from __future__ import annotations

from enum import StrEnum

import attr


@attr.s(auto_attribs=True, frozen=True)
class FetchResult:
    state: str


@attr.s(auto_attribs=True, frozen=True)
class DiffCommit:
    sha: str
    message: str


@attr.s(auto_attribs=True, frozen=True)
class DiffResult:
    commits: list[DiffCommit]
    current_ref: str
    remote_ref: str | None


class UpdateChannel(StrEnum):
    # No ref on the remote: follow the latest release tag. The only channel auto-updates run on.
    TAGS = "tags"
    # ``url#branch``: follow the branch tip.
    BRANCH = "branch"
    # ``url@ref``: a fixed tag or commit, never moves.
    PINNED = "pinned"


@attr.s(auto_attribs=True, frozen=True)
class RemoteInfo:
    url: str | None
    ref: str
    # For TAGS, ``ref`` is the resolved current release tag shown for information only; the dashboard must NOT
    # reconstruct a ``url@ref`` pin from it, or re-saving the remote would silently freeze the host on that tag.
    channel: UpdateChannel


@attr.s(auto_attribs=True, frozen=True)
class SwapStatus:
    # On-disk size of the managed swap file (the configured size, which survives
    # a swapoff). 0 when no swap file is present.
    size_bytes: int
    path: str
    # Whether the swap file is currently swapped on and available to the kernel.
    active: bool


@attr.s(auto_attribs=True, frozen=True)
class MigrationStatus:
    ok: bool
    reason: str
    message: str
    current_host_version: int
    expected_version: int
