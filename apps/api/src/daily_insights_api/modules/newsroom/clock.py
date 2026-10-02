"""Edition windows and daily deadlines in Asia/Taipei (spec §3)."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
CUTOFF = time(8, 0)
AUTO_PUBLISH = time(9, 0)
LATE_FILL_DEADLINE = time(12, 0)
PURGE_AT = time(3, 0)
TRIAGE_GRACE = timedelta(minutes=10)
BODY_RETENTION = timedelta(days=30)


def edition_date_for(seen_at: datetime) -> date:
    """The edition an article first seen at ``seen_at`` belongs to.

    Edition D collects ``[D-1 08:00, D 08:00)`` Taipei time, so anything seen at
    or after 08:00 belongs to the next day's edition.
    """
    local = seen_at.astimezone(TAIPEI)
    if local.time() >= CUTOFF:
        return local.date() + timedelta(days=1)
    return local.date()


def at_taipei(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=TAIPEI).astimezone(UTC)


def window(edition_date: date) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` UTC bounds of an edition's collection window."""
    return at_taipei(edition_date - timedelta(days=1), CUTOFF), at_taipei(edition_date, CUTOFF)


def auto_publish_at(edition_date: date) -> datetime:
    return at_taipei(edition_date, AUTO_PUBLISH)


def late_fill_deadline(edition_date: date) -> datetime:
    return at_taipei(edition_date, LATE_FILL_DEADLINE)


def taipei_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(TAIPEI).date()
