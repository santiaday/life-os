"""DB operations for the lifelog HTTP surface.

Uses the existing lifeos_core.db pool. All writes go through `tx()` so the
caller gets transactional semantics. The natural identity for an iOS event
is `source='ios_manual'` + `source_event_id=<uuid4>` — consistent with
existing sources.

Critical invariant: at most ONE open ios_manual session at any time.
`start_event` enforces this by closing any open row in the same transaction
that inserts the new one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from lifeos_core.db import tx

from .activity_types import get_activity_type
from .schemas import (
    EndEventRequest,
    EventContact,
    EventLocation,
    EventResponse,
    HealthResponse,
    StartEventRequest,
)

SOURCE = "ios_manual"
EVENT_TYPE = "activity"

# How long an open session lives before the stale closer auto-ends it.
# Must exceed the iOS Live Activity 8-hour wall (so the user has time to end
# manually after the LA dies) but not so long it fills /events/recent with
# obviously-broken rows.
STALE_AFTER_HOURS = 12

# How long after start to estimate the end. iOS sessions that go stale are
# usually evening events (sleep, lost track) — 4h is the median.
STALE_ESTIMATED_HOURS = 4


# ─── helpers ────────────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _resolve_started_at(req_started_at: datetime | None) -> datetime:
    if req_started_at is None:
        return _now_utc()
    # Normalize to UTC. Postgres timestamptz stores UTC regardless of input
    # tz, but psycopg renders it back in the connection's TZ — keeping the
    # input UTC keeps round-trip behavior predictable.
    if req_started_at.tzinfo is None:
        # iOS clients should send tz-aware. If not, assume UTC and log later.
        return req_started_at.replace(tzinfo=UTC)
    return req_started_at.astimezone(UTC)


def _row_to_response(row: dict) -> EventResponse:
    """Translate a DB row (with metadata JSONB) into the iOS-facing shape."""
    meta = row.get("metadata") or {}
    loc_raw = meta.get("location")
    location = EventLocation.model_validate(loc_raw) if loc_raw else None
    contacts_raw = meta.get("contacts") or []
    contacts = [EventContact.model_validate(c) for c in contacts_raw]
    return EventResponse(
        id=UUID(row["source_event_id"]),
        activity_type=meta.get("activity_type") or row["category"],
        title=row["title"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_seconds=row["duration_seconds"],
        location=location,
        contacts=contacts,
        notes=meta.get("notes"),
        focus_mode=meta.get("focus_mode"),
    )


def _build_metadata(req: StartEventRequest) -> dict:
    """JSONB payload for the events.metadata column. Mirrors the iOS shape so
    /events/recent can rehydrate without joining anything."""
    md: dict = {"activity_type": req.activity_type}
    if req.location is not None:
        md["location"] = req.location.model_dump()
    if req.contacts:
        md["contacts"] = [c.model_dump() for c in req.contacts]
    if req.notes:
        md["notes"] = req.notes
    if req.focus_mode:
        md["focus_mode"] = req.focus_mode
    if req.device:
        md["device"] = req.device
    return md


# ─── operations ─────────────────────────────────────────────────────────────


def start_event(req: StartEventRequest) -> EventResponse:
    """Open a new ios_manual session.

    Single transaction: closes any currently-open ios_manual events
    (idempotency / single-session invariant), then inserts the new row.
    Returns the inserted row in the iOS shape.

    `FOR UPDATE` on the close step means a concurrent /events/start race
    serializes — only one of two simultaneous starts will see an open
    session to close, which is correct.
    """
    activity = get_activity_type(req.activity_type)
    if activity is None:
        # Permissive: still accept unknown activity_type so the iOS app
        # doesn't break if the JSON drifts. Title falls back to the id.
        title = req.label or req.activity_type
        category = req.activity_type
    else:
        title = req.label or activity.label
        category = activity.label

    started_at = _resolve_started_at(req.started_at)
    new_id = uuid4()
    metadata = _build_metadata(req)

    with tx() as c, c.cursor() as cur:
        # 1. Lock + close any open ios_manual sessions. The annotation in
        #    metadata records why — useful when reconciling on the iOS side.
        cur.execute(
            """
            SELECT id, started_at, metadata
              FROM events
             WHERE source = %s AND ended_at IS NULL
             ORDER BY started_at DESC
             FOR UPDATE
            """,
            [SOURCE],
        )
        for stale in cur.fetchall():
            cur.execute(
                """
                UPDATE events
                   SET ended_at = %s,
                       metadata = COALESCE(metadata, '{}'::jsonb)
                                  || jsonb_build_object(
                                       'auto_closed', true,
                                       'auto_closed_at', %s,
                                       'auto_closed_reason', 'superseded'
                                     ),
                       updated_at = now()
                 WHERE id = %s
                """,
                [started_at, started_at, stale["id"]],
            )

        # 2. Insert the new row.
        cur.execute(
            """
            INSERT INTO events (
              source, source_event_id, event_type, category, title,
              started_at, ended_at, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, NULL, %s)
            RETURNING *
            """,
            [
                SOURCE,
                str(new_id),
                EVENT_TYPE,
                category,
                title,
                started_at,
                Jsonb(metadata),
            ],
        )
        row = cur.fetchone()
        assert row is not None
    return _row_to_response(row)


def close_event(req: EndEventRequest) -> EventResponse | None:
    """Close an open event by source_event_id. Returns None if the id doesn't
    exist or the event is already closed (the route maps that to 404 so the
    iOS app can recover by calling /events/active)."""
    ended_at = req.ended_at or _now_utc()
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)
    else:
        ended_at = ended_at.astimezone(UTC)

    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, started_at, metadata
              FROM events
             WHERE source = %s AND source_event_id = %s
             FOR UPDATE
            """,
            [SOURCE, str(req.event_id)],
        )
        row = cur.fetchone()
        if row is None:
            return None

        # Merge end-time notes into metadata (preserve any existing notes).
        current_meta = row.get("metadata") or {}
        if req.notes is not None:
            existing = current_meta.get("notes") or ""
            merged = f"{existing}\n{req.notes}".strip() if existing else req.notes
            current_meta = {**current_meta, "notes": merged}

        # Defensive: if ended_at would be <= started_at, clamp to
        # started_at + 1s. Happens when the iOS clock is behind the server
        # (unlikely but possible with a manually rewound clock).
        started_at = row["started_at"]
        if ended_at <= started_at:
            ended_at = started_at.replace(microsecond=0) + timedelta(seconds=1)

        cur.execute(
            """
            UPDATE events
               SET ended_at = %s,
                   metadata = %s,
                   updated_at = now()
             WHERE id = %s
             RETURNING *
            """,
            [ended_at, Jsonb(current_meta), row["id"]],
        )
        updated = cur.fetchone()
        assert updated is not None
    return _row_to_response(updated)


def fetch_active() -> EventResponse | None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT *
              FROM events
             WHERE source = %s AND ended_at IS NULL
             ORDER BY started_at DESC
             LIMIT 1
            """,
            [SOURCE],
        )
        row = cur.fetchone()
    return _row_to_response(row) if row else None


def fetch_recent(*, limit: int = 50, before: datetime | None = None) -> list[EventResponse]:
    """Most recent ios_manual events, started_at DESC. Excludes the open
    session — the iOS app shows that separately as the active card."""
    sql = [
        "SELECT * FROM events",
        "WHERE source = %s AND ended_at IS NOT NULL",
    ]
    params: list = [SOURCE]
    if before is not None:
        if before.tzinfo is None:
            before = before.replace(tzinfo=UTC)
        sql.append("AND started_at < %s")
        params.append(before)
    sql.append("ORDER BY started_at DESC LIMIT %s")
    params.append(min(max(limit, 1), 200))
    with tx() as c, c.cursor() as cur:
        cur.execute("\n".join(sql), params)
        rows = cur.fetchall()
    return [_row_to_response(r) for r in rows]


def fetch_by_id(source_event_id: UUID) -> EventResponse | None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM events
             WHERE source = %s AND source_event_id = %s
            """,
            [SOURCE, str(source_event_id)],
        )
        row = cur.fetchone()
    return _row_to_response(row) if row else None


def health() -> HealthResponse:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE ended_at IS NULL)        AS open_count,
              COUNT(*) FILTER (WHERE day = CURRENT_DATE)      AS today_count,
              MAX(started_at)                                  AS last_started_at
            FROM events
            WHERE source = %s
            """,
            [SOURCE],
        )
        row = cur.fetchone()
    return HealthResponse(
        open_session_count=int(row["open_count"]) if row else 0,
        events_today=int(row["today_count"]) if row else 0,
        last_event_at=row["last_started_at"] if row else None,
    )


# ─── stale closer ───────────────────────────────────────────────────────────


def close_stale_events() -> int:
    """Auto-close any ios_manual event open for more than STALE_AFTER_HOURS,
    estimating an end at started_at + STALE_ESTIMATED_HOURS. Returns the
    count of rows closed.

    Run from the scheduler every 30 min. iOS Live Activities die after 8h
    and the user often forgets to end manually after that — this keeps the
    timeline honest without injecting fake durations.
    """
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE events
               SET ended_at = started_at + (%s || ' hours')::interval,
                   metadata = COALESCE(metadata, '{}'::jsonb)
                              || jsonb_build_object(
                                   'auto_closed', true,
                                   'auto_closed_at', now(),
                                   'auto_closed_reason', 'stale_12h'
                                 ),
                   updated_at = now()
             WHERE source = %s
               AND ended_at IS NULL
               AND started_at < now() - (%s || ' hours')::interval
             RETURNING id
            """,
            [STALE_ESTIMATED_HOURS, SOURCE, STALE_AFTER_HOURS],
        )
        rows = cur.fetchall()
    return len(rows)
