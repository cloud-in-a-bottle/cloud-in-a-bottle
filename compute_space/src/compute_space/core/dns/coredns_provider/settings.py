"""What the compute space hands the provider so it can run.

A plain value, constructed once and passed in: paths to write to, and the address to answer on.
Having it means this package imports nothing from the application around it, and the file layout
below is the provider's own rather than something ``Config`` has to know.
"""

from __future__ import annotations

from pathlib import Path

import attr


@attr.s(auto_attribs=True, frozen=True)
class DnsSettings:
    """The provider's surroundings."""

    # Where the generated CoreDNS config goes.
    corefile_path: Path
    # The primary zone's file keeps this legacy path; every other zone gets one under zones_dir.
    zonefile_path: Path
    zones_dir: Path
    # Only a fallback for the address CoreDNS binds, used where the default-route probe fails.
    # Nothing in a zone file comes from here -- the records that route the space are published
    # like any other.
    public_ip: str
    # What the container-facing view binds, or None where that interface doesn't exist (dev/CI),
    # which drops the view rather than emitting a Corefile CoreDNS would refuse to start on.
    container_gateway_ip: str | None = None

    def zonefile_path_for(self, zone: str, is_primary: bool) -> Path:
        """Where a zone's generated file goes.

        Each zone needs its own file, since a zone is only authoritative for what is in it.  Any
        port is stripped so none ends up in a filename.
        """
        if is_primary:
            return self.zonefile_path
        return self.zones_dir / f"{zone.split(':')[0]}.zone"
