"""Google Calendar API v3 client.

Uses the official google-api-python-client. Wraps:
- syncToken-based incremental fetch (preferred)
- timeMin/timeMax full-window fetch (initial sync, or after 410 GONE)
- pagination via pageToken

Reference: https://developers.google.com/calendar/api/v3/reference/events/list
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ingest_calendar.oauth import credentials
from lifeos_core.db import tx
from lifeos_core.logging import get_logger

log = get_logger(__name__)

# Default initial-sync window. Subsequent runs use the syncToken so window
# size stops mattering.
DEFAULT_INITIAL_PAST_DAYS = 90
DEFAULT_INITIAL_FUTURE_DAYS = 30


class CalendarClient:
    def __init__(self) -> None:
        self._service = build("calendar", "v3", credentials=credentials(), cache_discovery=False)

    def list_events(
        self,
        calendar_id: str,
        *,
        sync_token: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> tuple[Iterator[dict], str | None]:
        """Yield events for `calendar_id`. Returns (event_iter, next_sync_token).

        - With `sync_token`: incremental — only changed events since last sync.
          Cannot be combined with timeMin/timeMax/orderBy. May raise 410 GONE,
          in which case caller must do a full re-sync.
        - Without `sync_token`: full sync over [time_min, time_max].

        `singleEvents=true` expands recurring events into individual occurrences,
        which is what we want — every actual time on the calendar is one row.
        """
        events: list[dict] = []
        page_token: str | None = None
        next_sync: str | None = None

        while True:
            params: dict = {
                "calendarId": calendar_id,
                "singleEvents": True,
                "showDeleted": True,  # so we can mark cancellations
                "maxResults": 250,
            }
            if sync_token is not None:
                params["syncToken"] = sync_token
            else:
                params["timeMin"] = _rfc3339(time_min)
                params["timeMax"] = _rfc3339(time_max)
                params["orderBy"] = "startTime"
            if page_token is not None:
                params["pageToken"] = page_token

            resp = self._service.events().list(**params).execute()
            events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if page_token is None:
                next_sync = resp.get("nextSyncToken")
                break

        return iter(events), next_sync


# ---- syncToken state helpers ------------------------------------------------
def get_sync_token(calendar_id: str) -> str | None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT sync_token FROM calendar_sync_state WHERE calendar_id = %s",
            [calendar_id],
        )
        row = cur.fetchone()
        return row["sync_token"] if row else None


def save_sync_token(calendar_id: str, sync_token: str | None, *, full_sync: bool = False) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO calendar_sync_state (calendar_id, sync_token, last_full_sync_at, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (calendar_id) DO UPDATE SET
              sync_token = EXCLUDED.sync_token,
              last_full_sync_at = COALESCE(EXCLUDED.last_full_sync_at, calendar_sync_state.last_full_sync_at),
              updated_at = now()
            """,
            [calendar_id, sync_token, datetime.now(timezone.utc) if full_sync else None],
        )


def is_gone(exc: BaseException) -> bool:
    """410 GONE means our syncToken expired — do a full re-sync."""
    return isinstance(exc, HttpError) and exc.resp.status == 410


def _rfc3339(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
