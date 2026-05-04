"""Helpers for the unified `events` timeline table.

Every source (Whoop, ActivityWatch, future iOS) lands rows here with
(source, source_event_id) as the natural key. The calendar_sync service
reads `synced_at IS NULL` rows and pushes them to Google Calendar.

`upsert_events` deliberately does NOT touch calendar_* / synced_at columns
on update — those are owned by the calendar_sync writer. If a source
re-emits a row whose ended_at changed (e.g. an ActivityWatch block extending
across a sync boundary), we only update the source-owned columns; the
calendar writer detects the change via `updated_at > synced_at` and
PATCHes the calendar event. (See calendar_sync.sync.)
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from lifeos_core.db import tx

# Columns the source owns. Calendar-sync state is set elsewhere.
_SOURCE_COLS = (
    "source",
    "source_event_id",
    "event_type",
    "category",
    "title",
    "started_at",
    "ended_at",
    "metadata",
)
# On a re-emit, refresh these only — leaves calendar_id/calendar_event_id/
# synced_at intact so the writer can decide whether to PATCH.
_UPDATE_COLS = ("event_type", "category", "title", "started_at", "ended_at", "metadata", "updated_at")


def make_aw_source_event_id(hostname: str, started_at_iso: str) -> str:
    """Stable id for ActivityWatch work blocks. The hostname pins the
    laptop, started_at pins the block. Re-running the daemon over the same
    window produces the same id and updates rather than duplicates."""
    h = hashlib.sha256(f"{hostname}|{started_at_iso}".encode()).hexdigest()
    return h[:16]


def upsert_events(
    rows: Sequence[dict],
    *,
    connection: psycopg.Connection | None = None,
) -> int:
    """Upsert a batch of event rows. Returns count written.

    Each row must have keys: source, source_event_id, event_type, category,
    title, started_at, ended_at. `metadata` is optional (defaults to {}).

    Wraps metadata in psycopg's Jsonb so callers can pass plain dicts.
    """
    if not rows:
        return 0

    sql = """
        INSERT INTO events
          (source, source_event_id, event_type, category, title,
           started_at, ended_at, metadata, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (source, source_event_id) DO UPDATE SET
          event_type = EXCLUDED.event_type,
          category   = EXCLUDED.category,
          title      = EXCLUDED.title,
          started_at = EXCLUDED.started_at,
          ended_at   = EXCLUDED.ended_at,
          metadata   = EXCLUDED.metadata,
          updated_at = now()
    """

    def _execute(c: psycopg.Connection) -> int:
        count = 0
        with c.cursor() as cur:
            for r in rows:
                meta = r.get("metadata") or {}
                cur.execute(
                    sql,
                    [
                        r["source"],
                        r["source_event_id"],
                        r["event_type"],
                        r["category"],
                        r["title"],
                        r["started_at"],
                        r["ended_at"],
                        Jsonb(meta) if not isinstance(meta, Jsonb) else meta,
                    ],
                )
                count += cur.rowcount
        return count

    if connection is not None:
        return _execute(connection)
    with tx() as c:
        return _execute(c)


def fetch_unsynced(limit: int = 200) -> list[dict]:
    """Return events pending calendar sync. Two cases:

    1. Never synced (synced_at IS NULL).
    2. Synced before but updated since (updated_at > synced_at) — these
       need a PATCH on the calendar side.

    Ordered by started_at so calendar_event_ids tend to be allocated in
    chronological order (cosmetic, not load-bearing)."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM events
            WHERE synced_at IS NULL
               OR updated_at > synced_at
            ORDER BY started_at
            LIMIT %s
            """,
            [limit],
        )
        return list(cur.fetchall())


def mark_synced(
    event_id: int,
    *,
    calendar_id: str,
    calendar_event_id: str,
    connection: psycopg.Connection | None = None,
) -> None:
    """Record a successful Google Calendar write."""
    sql = """
        UPDATE events SET
          calendar_id = %s,
          calendar_event_id = %s,
          synced_at = now()
        WHERE id = %s
    """

    def _execute(c: psycopg.Connection) -> None:
        with c.cursor() as cur:
            cur.execute(sql, [calendar_id, calendar_event_id, event_id])

    if connection is not None:
        _execute(connection)
        return
    with tx() as c:
        _execute(c)


def categories_in_use() -> Iterable[str]:
    """Distinct categories present in events. Used to validate the calendar
    map config (any category we emit must have a matching calendar id)."""
    with tx() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT category FROM events ORDER BY category")
        return [row["category"] for row in cur.fetchall()]
