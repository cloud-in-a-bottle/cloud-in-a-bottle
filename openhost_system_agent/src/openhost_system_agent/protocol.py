from __future__ import annotations

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


@attr.s(auto_attribs=True, frozen=True)
class RemoteInfo:
    url: str | None
    ref: str
    # True only when the instance is pinned to a target ref (git config
    # openhost.target-ref). When False, ``ref`` is the resolved current release
    # tag shown for information only; the dashboard must NOT reconstruct a
    # ``url@ref`` pin from it, or re-saving an unpinned remote would silently
    # freeze the host on the current tag.
    pinned: bool = False


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
