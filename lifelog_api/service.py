"""DB operations for the lifelog HTTP surface.

Uses the existing lifeos_core.db pool. Multi-tenant by carrying a
`user_id` parameter on every public function — derived from the bearer
token via lifelog_api.auth.current_user_id and forwarded by the route
layer.

Source = 'ios_manual' for everything written from the iOS app (both
sessions and annotations). Distinguished by `event_kind`:
  - session    : has start + end, supports Live Activity
  - annotation : single moment (ended_at == started_at), no Live Activity

Critical invariant: at most ONE open ios_manual SESSION per user at any
time. annotations don't participate — they're closed at insert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lifeos_core.db import tx

from .activity_types import get_activity_type
from .schemas import (
    AnnotateEventRequest,
    EndEventRequest,
    EventContact,
    EventLocation,
    EventResponse,
    HealthResponse,
    StartEventRequest,
    UpdateEventRequest,
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


def _resolve_datetime(value: datetime | None) -> datetime:
    """Normalize an incoming datetime to UTC, defaulting to now() if
    None. The iOS app sends tz-aware datetimes; we tolerate naive input
    by assuming UTC."""
    if value is None:
        return _now_utc()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
        event_kind=row.get("event_kind") or "session",
        location=location,
        contacts=contacts,
        notes=meta.get("notes"),
        focus_mode=meta.get("focus_mode"),
    )


def _build_metadata(
    *,
    activity_type: str,
    location: EventLocation | None = None,
    contacts: list[EventContact] | None = None,
    notes: str | None = None,
    focus_mode: str | None = None,
    device: str | None = None,
) -> dict:
    """JSONB payload for the events.metadata column. Mirrors the iOS
    shape so /events/recent can rehydrate without joining anything."""
    md: dict = {"activity_type": activity_type}
    if location is not None:
        md["location"] = location.model_dump()
    if contacts:
        md["contacts"] = [c.model_dump() for c in contacts]
    if notes:
        md["notes"] = notes
    if focus_mode:
        md["focus_mode"] = focus_mode
    if device:
        md["device"] = device
    return md


def _resolve_title_and_category(
    user_id: str, slug: str, override_label: str | None
) -> tuple[str, str]:
    """Look up the activity in the DB to derive the human-readable title
    + category. Tolerant of unknown slugs (the iOS app might send a slug
    the server doesn't have yet, e.g. mid-deploy) — falls back to the
    slug for both fields."""
    activity = get_activity_type(user_id, slug)
    if activity is None:
        title = override_label or slug
        category = slug
    else:
        title = override_label or activity.label
        category = activity.label
    return title, category


# ─── operations: sessions ───────────────────────────────────────────────────


def start_event(user_id: str, req: StartEventRequest) -> EventResponse:
    """Open a new ios_manual session.

    Single transaction: closes any currently-open ios_manual sessions
    for this user (single-active-session invariant), then inserts the
    new row. `FOR UPDATE` on the close step serializes concurrent
    starts — only one of two simultaneous starts will see an open
    session to close.
    """
    title, category = _resolve_title_and_category(
        user_id, req.activity_type, req.label
    )
    started_at = _resolve_datetime(req.started_at)
    new_id = uuid4()
    metadata = _build_metadata(
        activity_type=req.activity_type,
        location=req.location,
        contacts=req.contacts,
        notes=req.notes,
        focus_mode=req.focus_mode,
        device=req.device,
    )

    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        # 1. Lock + close any open ios_manual sessions for THIS user.
        #    Annotations don't participate (their ended_at is non-null).
        cur.execute(
            """
            SELECT id
              FROM events
             WHERE source = %s
               AND user_id = %s
               AND event_kind = 'session'
               AND ended_at IS NULL
             FOR UPDATE
            """,
            [SOURCE, user_id],
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

        # 2. Insert the new session row.
        cur.execute(
            """
            INSERT INTO events (
              source, source_event_id, event_type, category, title,
              started_at, ended_at, metadata, user_id, event_kind
            )
            VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, 'session')
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
                user_id,
            ],
        )
        row = cur.fetchone()
        assert row is not None
    return _row_to_response(row)


def close_event(user_id: str, req: EndEventRequest) -> EventResponse | None:
    """Close an open session by source_event_id. Returns None if the id
    doesn't exist or the event is already closed (route maps to 404)."""
    ended_at = _resolve_datetime(req.ended_at)

    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, started_at, metadata
              FROM events
             WHERE source = %s
               AND user_id = %s
               AND source_event_id = %s
             FOR UPDATE
            """,
            [SOURCE, user_id, str(req.event_id)],
        )
        row = cur.fetchone()
        if row is None:
            return None

        current_meta = row.get("metadata") or {}
        if req.notes is not None:
            existing = current_meta.get("notes") or ""
            merged = f"{existing}\n{req.notes}".strip() if existing else req.notes
            current_meta = {**current_meta, "notes": merged}

        # Clamp ended_at >= started_at + 1s to avoid the rare iOS-clock-
        # behind-server case generating negative durations.
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


# ─── operations: annotations ────────────────────────────────────────────────


def annotate(user_id: str, req: AnnotateEventRequest) -> EventResponse:
    """Log a single-moment event. Inserts an `events` row with
    `ended_at == started_at` and `event_kind = 'annotation'`. Doesn't
    interact with open sessions — annotations and sessions coexist.

    Generated `duration_seconds` will be 0 for annotations. Calendar
    sync excludes annotations (see fetch_unsynced)."""
    title, category = _resolve_title_and_category(
        user_id, req.activity_type, None
    )
    occurred_at = _resolve_datetime(req.occurred_at)
    new_id = uuid4()
    metadata = _build_metadata(
        activity_type=req.activity_type,
        location=req.location,
        contacts=req.contacts,
        notes=req.notes,
        focus_mode=None,
        device=req.device,
    )

    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO events (
              source, source_event_id, event_type, category, title,
              started_at, ended_at, metadata, user_id, event_kind
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'annotation')
            RETURNING *
            """,
            [
                SOURCE,
                str(new_id),
                EVENT_TYPE,
                category,
                title,
                occurred_at,
                occurred_at,            # ended_at == started_at
                Jsonb(metadata),
                user_id,
            ],
        )
        row = cur.fetchone()
        assert row is not None
    return _row_to_response(row)


# ─── operations: reads ──────────────────────────────────────────────────────


def fetch_active(user_id: str) -> EventResponse | None:
    """Return this user's currently-open SESSION, if any. Annotations
    aren't sessions and never appear here."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT *
              FROM events
             WHERE source = %s
               AND user_id = %s
               AND event_kind = 'session'
               AND ended_at IS NULL
             ORDER BY started_at DESC
             LIMIT 1
            """,
            [SOURCE, user_id],
        )
        row = cur.fetchone()
    return _row_to_response(row) if row else None


def fetch_recent(
    user_id: str,
    *,
    limit: int = 50,
    before: datetime | None = None,
    kind: str | None = None,
) -> list[EventResponse]:
    """Most recent ios_manual events for this user, started_at DESC.

    Excludes the open session (it's surfaced separately at /active).
    Optional kind filter ('session' or 'annotation') for views that want
    one or the other."""
    sql_parts = [
        "SELECT * FROM events",
        "WHERE source = %s",
        "  AND user_id = %s",
        # Annotations have ended_at = started_at so they're always
        # considered "ended". Sessions still need the IS NOT NULL guard.
        "  AND (event_kind = 'annotation' OR ended_at IS NOT NULL)",
    ]
    params: list = [SOURCE, user_id]
    if kind:
        sql_parts.append("AND event_kind = %s")
        params.append(kind)
    if before is not None:
        if before.tzinfo is None:
            before = before.replace(tzinfo=UTC)
        sql_parts.append("AND started_at < %s")
        params.append(before)
    sql_parts.append("ORDER BY started_at DESC LIMIT %s")
    params.append(min(max(limit, 1), 200))
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("\n".join(sql_parts), params)
        rows = cur.fetchall()
    return [_row_to_response(r) for r in rows]


def update_event(
    user_id: str, source_event_id: UUID, req: UpdateEventRequest
) -> EventResponse | None:
    """PATCH an existing event. Used to fix times, edit notes, swap
    location/contacts after the fact. Returns None if the event isn't
    found (route maps to 404).

    Rules:
      - Sessions: started_at and ended_at independently editable. Server
        clamps ended_at >= started_at + 1s.
      - Annotations: ended_at is always pinned to == started_at, so any
        provided ended_at is ignored. `occurred_at` (preferred name for
        annotation timestamps) overrides started_at.
      - Notes, location, contacts are merged into metadata.
    """
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, started_at, ended_at, metadata, event_kind
              FROM events
             WHERE source = %s AND user_id = %s AND source_event_id = %s
             FOR UPDATE
            """,
            [SOURCE, user_id, str(source_event_id)],
        )
        row = cur.fetchone()
        if row is None:
            return None

        kind: str = row["event_kind"]
        new_started = row["started_at"]
        new_ended = row["ended_at"]

        # Annotations: `occurred_at` overrides; ended stays pinned.
        if kind == "annotation":
            if req.occurred_at is not None:
                new_started = _resolve_datetime(req.occurred_at)
                new_ended = new_started
            elif req.started_at is not None:
                new_started = _resolve_datetime(req.started_at)
                new_ended = new_started
            # Any provided ended_at is intentionally ignored for annotations.
        else:
            if req.started_at is not None:
                new_started = _resolve_datetime(req.started_at)
            if req.ended_at is not None:
                new_ended = _resolve_datetime(req.ended_at)
            # Clamp ended_at strictly after started_at.
            if new_ended is not None and new_ended <= new_started:
                new_ended = new_started + timedelta(seconds=1)

        # Merge metadata edits.
        current_meta: dict = dict(row.get("metadata") or {})
        if req.notes is not None:
            if req.notes == "":
                current_meta.pop("notes", None)
            else:
                current_meta["notes"] = req.notes
        if req.location is not None:
            current_meta["location"] = req.location.model_dump()
        if req.contacts is not None:
            if not req.contacts:
                current_meta.pop("contacts", None)
            else:
                current_meta["contacts"] = [c.model_dump() for c in req.contacts]

        cur.execute(
            """
            UPDATE events
               SET started_at = %s,
                   ended_at   = %s,
                   metadata   = %s,
                   updated_at = now()
             WHERE id = %s
             RETURNING *
            """,
            [new_started, new_ended, Jsonb(current_meta), row["id"]],
        )
        updated = cur.fetchone()
        assert updated is not None
    return _row_to_response(updated)


def delete_event(user_id: str, source_event_id: UUID) -> bool:
    """Hard-delete an event row. Returns True if a row was removed,
    False if no event matched. Calendar sync will see the deletion via
    its own reconcile pass (synced_at-aware delete is handled
    elsewhere; for now the iOS app is the source of truth for ios_manual
    events and we just drop the row)."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            DELETE FROM events
             WHERE source = %s AND user_id = %s AND source_event_id = %s
             RETURNING id
            """,
            [SOURCE, user_id, str(source_event_id)],
        )
        return cur.fetchone() is not None


def fetch_by_id(user_id: str, source_event_id: UUID) -> EventResponse | None:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT * FROM events
             WHERE source = %s
               AND user_id = %s
               AND source_event_id = %s
            """,
            [SOURCE, user_id, str(source_event_id)],
        )
        row = cur.fetchone()
    return _row_to_response(row) if row else None


def health(user_id: str) -> HealthResponse:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE event_kind = 'session' AND ended_at IS NULL
              ) AS open_count,
              COUNT(*) FILTER (WHERE day = CURRENT_DATE)  AS today_count,
              MAX(started_at)                              AS last_started_at
            FROM events
            WHERE source = %s
              AND user_id = %s
            """,
            [SOURCE, user_id],
        )
        row = cur.fetchone()
    return HealthResponse(
        open_session_count=int(row["open_count"]) if row else 0,
        events_today=int(row["today_count"]) if row else 0,
        last_event_at=row["last_started_at"] if row else None,
    )


# ─── stale closer ───────────────────────────────────────────────────────────


def close_stale_events() -> int:
    """Auto-close any ios_manual SESSION open for more than
    STALE_AFTER_HOURS, estimating an end at started_at +
    STALE_ESTIMATED_HOURS. Returns the count of rows closed.

    Annotations are exempt — their ended_at is always set at insert.
    Run from the scheduler every 30 min.

    Multi-tenant note: this closes ALL stale sessions across users at
    once. When we switch to per-tenant policies (different stale
    thresholds per user) this gets re-scoped — but for now a global
    pass is correct + cheap.
    """
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
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
               AND event_kind = 'session'
               AND ended_at IS NULL
               AND started_at < now() - (%s || ' hours')::interval
             RETURNING id
            """,
            [STALE_ESTIMATED_HOURS, SOURCE, STALE_AFTER_HOURS],
        )
        rows = cur.fetchall()
    return len(rows)
