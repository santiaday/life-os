"""Calendar ingestion: API → raw_calendar_event → fact_calendar_event.

Per-calendar incremental sync via syncToken with automatic full-resync
fallback on 410 GONE. Each ingestion_runs row corresponds to one calendar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from ingest_calendar import transforms
from ingest_calendar.client import (
    DEFAULT_INITIAL_FUTURE_DAYS,
    DEFAULT_INITIAL_PAST_DAYS,
    CalendarClient,
    get_sync_token,
    is_gone,
    save_sync_token,
)
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.settings import settings
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)


def ingest_calendar(calendar_id: str, *, force_full: bool = False) -> int:
    """Ingest one calendar. Returns count upserted."""
    sync_token = None if force_full else get_sync_token(calendar_id)

    with ingestion_run(
        "calendar", "events", calendar_id=calendar_id, mode="incremental" if sync_token else "full"
    ) as run:
        client = CalendarClient()

        if sync_token:
            try:
                events_iter, next_sync = client.list_events(calendar_id, sync_token=sync_token)
            except Exception as e:
                if is_gone(e):
                    log.warning("calendar.sync_token_gone", calendar_id=calendar_id)
                    sync_token = None  # fall through to full sync
                else:
                    raise

        if not sync_token:
            now = datetime.now(UTC)
            time_min = now - timedelta(days=DEFAULT_INITIAL_PAST_DAYS)
            time_max = now + timedelta(days=DEFAULT_INITIAL_FUTURE_DAYS)
            events_iter, next_sync = client.list_events(
                calendar_id, time_min=time_min, time_max=time_max
            )

        events = list(events_iter)
        run.fetched(len(events))
        if not events:
            if next_sync:
                save_sync_token(calendar_id, next_sync, full_sync=sync_token is None)
            return 0

        # Inject calendar_id so transform_event can put it on the row.
        for e in events:
            e["_calendar_id"] = calendar_id

        # ---- raw upsert -----------------------------------------------------
        raw_rows = [
            {
                "calendar_id": calendar_id,
                "event_id": e["id"],
                "etag": e.get("etag"),
                "payload": Jsonb(e),
            }
            for e in events
        ]
        with tx() as c:
            upsert_rows(
                "raw_calendar_event",
                raw_rows,
                conflict_cols=["calendar_id", "event_id"],
                update_cols=["etag", "payload", "fetched_at"],
                connection=c,
            )

            # ---- fact upsert ------------------------------------------------
            id_map = _raw_id_map(c, calendar_id, [e["id"] for e in events])
            fact_rows: list[dict] = []
            internal = settings.internal_email_domains
            for e in events:
                # Skip cancelled events whose start/end was never set.
                row = transforms.transform_event(e, internal_domains=internal)
                if row is None:
                    continue
                row["raw_id"] = id_map.get((calendar_id, e["id"]))
                row["updated_at"] = datetime.now(UTC)
                fact_rows.append(row)

            if fact_rows:
                upsert_rows(
                    "fact_calendar_event",
                    fact_rows,
                    conflict_cols=["calendar_id", "event_id"],
                    connection=c,
                )

        if next_sync:
            save_sync_token(calendar_id, next_sync, full_sync=sync_token is None)

        run.upserted(len(fact_rows))
        return len(fact_rows)


def run_all(*, force_full: bool = False) -> dict:
    """Ingest every calendar listed in GOOGLE_CALENDAR_IDS."""
    out: dict[str, int | str] = {}
    for cal_id in settings.calendar_ids:
        try:
            out[cal_id] = ingest_calendar(cal_id, force_full=force_full)
        except Exception as e:
            log.exception("calendar.pipeline.failed", calendar_id=cal_id)
            out[cal_id] = f"FAILED: {type(e).__name__}: {e}"
    return out


def _raw_id_map(connection, calendar_id: str, event_ids: list[str]) -> dict:
    """Resolve raw_calendar_event.id for (calendar_id, event_id) pairs."""
    if not event_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT calendar_id, event_id, id
            FROM raw_calendar_event
            WHERE calendar_id = %s AND event_id = ANY(%s)
            """,
            [calendar_id, event_ids],
        )
        return {(r["calendar_id"], r["event_id"]): r["id"] for r in cur.fetchall()}
