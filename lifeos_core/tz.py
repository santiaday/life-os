"""Timezone helpers.

Convention (per SPEC.md §0):
- All `*_ts` columns are TIMESTAMPTZ in UTC.
- All `day` columns are local dates, computed via SQL `local_date(ts)` or
  Python `local_date(ts)` for symmetry.
- `LOCAL_TZ` is configurable via env, defaults to America/New_York.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from lifeos_core.settings import settings


def local_tz() -> ZoneInfo:
    return ZoneInfo(settings.LOCAL_TZ)


def to_utc(dt: datetime) -> datetime:
    """Coerce any datetime to UTC. Naive datetimes are assumed local."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(UTC)


def from_local_naive(dt: datetime) -> datetime:
    """Treat a naive datetime as local-tz wall clock and return UTC."""
    return dt.replace(tzinfo=local_tz()).astimezone(UTC)


def local_date(ts: datetime) -> date:
    """The local date a UTC timestamp falls on."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(local_tz()).date()


def local_midnight_utc(d: date) -> datetime:
    """Return UTC instant corresponding to local 00:00 on `d`. Used for
    all-day calendar events whose Whoop/Cronometer/Calendar source returns a
    bare date."""
    return datetime.combine(d, time.min, tzinfo=local_tz()).astimezone(UTC)
