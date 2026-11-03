from __future__ import annotations

import tomllib
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import attr
import httpx

from compute_space.core.logging import logger

# Written by a hosting platform that sleeps idle instances (in cloud-init vendor-data). Absent everywhere else.
PLATFORM_FILE = Path("/etc/bottle/platform.toml")

_TIMEOUT_S = 10.0


@attr.s(auto_attribs=True, frozen=True)
class WakeApi:
    """The platform's API for keeping a sleeping instance's scheduled work running: when to wake it next, and how
    long to keep it awake while a job runs."""

    url: str
    token: str

    @staticmethod
    def load(path: Path | None = None) -> WakeApi | None:
        path = path or PLATFORM_FILE
        if not path.exists():
            return None
        wake = tomllib.loads(path.read_text())["wake"]
        return WakeApi(url=wake["url"].rstrip("/"), token=wake["token"])

    async def _send(self, method: str, path: str, body: dict[str, float | None]) -> None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.request(
                method, f"{self.url}{path}", json=body, headers={"Authorization": f"Bearer {self.token}"}
            )
            resp.raise_for_status()

    async def set_wake_at(self, at: datetime | None) -> None:
        await self._send("PUT", "/wake", {"wake_at": at.timestamp() if at else None})

    async def hold_awake(self, duration: timedelta) -> None:
        await self._send("POST", "/busy", {"seconds": duration.total_seconds()})


async def hold_awake(duration: timedelta) -> None:
    """Ask the platform (if any) not to put the instance to sleep for `duration`, e.g. while a scheduled job runs.
    Never raises: a job must not fail because the platform couldn't be reached."""
    api = WakeApi.load()
    if api is None:
        return
    try:
        await api.hold_awake(duration)
    except httpx.HTTPError as e:
        logger.warning(f"could not ask the platform to keep this instance awake: {e}")
