from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta

# How long after the scheduled time an update may still start. Short on purpose: an instance that was down at the
# scheduled time waits for the next day rather than restarting right after it comes back.
WINDOW = timedelta(minutes=5)


def due_slot(now: datetime, time_utc: time, last_attempt_at: datetime | None) -> datetime | None:
    """The scheduled time an update should run for now, or None if nothing is due."""
    slot = datetime.combine(now.date(), time_utc, tzinfo=UTC)
    if slot > now:
        slot -= timedelta(days=1)
    if now - slot > WINDOW:
        return None
    if last_attempt_at is not None and last_attempt_at >= slot:
        return None
    return slot
