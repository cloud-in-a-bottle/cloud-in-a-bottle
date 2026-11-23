from collections import deque
from time import monotonic

import attr


@attr.s(auto_attribs=True)
class RateLimiter:
    """Allows ``limit`` hits per key in any ``window_seconds``. In memory: the router is a single process."""

    limit: int
    window_seconds: float
    _hits: dict[str, deque[float]] = attr.ib(factory=dict, init=False)

    def _recent(self, key: str) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= monotonic() - self.window_seconds:
            hits.popleft()
        return hits

    def is_limited(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def hit(self, key: str) -> None:
        if len(self._hits) > 10_000:
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > monotonic() - self.window_seconds}
        self._recent(key).append(monotonic())
