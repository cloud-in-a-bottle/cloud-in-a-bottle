from datetime import UTC
from datetime import datetime

# In memory only: writing on every request would cost more than it's worth, and the host treats an instance that just
# started as active anyway.
_last_valid_request_at: datetime | None = None


def record_valid_request() -> None:
    """Note an inbound request from someone real: one with valid auth, or to a path an app serves publicly. Scanners
    probing for paths that don't exist never get here, so they don't keep the instance awake."""
    global _last_valid_request_at
    _last_valid_request_at = datetime.now(UTC)


def last_valid_request_at() -> datetime | None:
    return _last_valid_request_at
