"""DB-backed activity-type catalog.

Reads/writes `lifelog_activity_types`. Multi-tenant via the `user_id`
column on every query — when this app moves to its own multi-tenant DB,
the schema is already correct.

Color derivation: the wire format includes a fallback hex string for
clients that don't do OKLCH. We compute it from `hue` here so the source
of truth (the DB row) is a single int. The fallback is approximate — the
iOS app re-derives the gradient/tint from `hue` directly using its OKLCH
helpers, so the hex here is only seen by hypothetical non-iOS clients.

JSON file `activity_types.json` is no longer load-bearing — kept as a
seed reference for the migration and as a doc artifact. The iOS app's
bundled copy is now treated as a first-run fallback for the case where
the device has never reached the server.
"""

from __future__ import annotations

import colorsys
import re

from psycopg.rows import dict_row

from lifeos_core.db import tx

from .schemas import (
    ActivityType,
    CreateActivityTypeRequest,
    UpdateActivityTypeRequest,
)

# Updatable columns. `slug`, `is_custom`, `user_id`, `created_at` are
# off-limits to UPDATE: slug is the identity, is_custom is server-set,
# user_id is auth-scoped, and created_at is immutable history.
_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "label", "emoji", "hue", "kind", "focus_mode",
    "default_capture_location", "default_capture_contacts",
    "live_activity_show_timer", "is_pinned", "sort_order",
)


def _hue_to_hex(hue: int, *, dark: bool = False) -> str:
    """Cheap fallback hex from an OKLCH hue. The iOS client doesn't use
    this — it re-derives from hue via OKLCH. We approximate with HSV at
    decent saturation so non-iOS clients still see a sensible color."""
    h = (hue % 360) / 360.0
    s = 0.55
    v = 0.85 if dark else 0.65
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return f"{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"


def _slug_from_label(label: str) -> str:
    """Generate a slug from a user-entered label. lowercase, ASCII-only,
    underscore-separated. Matches the seed slugs ('watching_tv').

    Collision handling lives at the caller (route layer): we raise on
    UNIQUE(user_id, slug) violation rather than silently appending a
    suffix, so the user sees a real "name already used" message."""
    s = label.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_") or "activity"


def _row_to_schema(row: dict) -> ActivityType:
    return ActivityType(
        id=row["slug"],
        label=row["label"],
        emoji=row["emoji"],
        hue=row["hue"],
        color=_hue_to_hex(row["hue"], dark=False),
        color_dark=_hue_to_hex(row["hue"], dark=True),
        kind=row["kind"],
        focus_mode=row["focus_mode"],
        default_capture_location=row["default_capture_location"],
        default_capture_contacts=row["default_capture_contacts"],
        live_activity_show_timer=row["live_activity_show_timer"],
        is_pinned=row["is_pinned"],
        is_custom=row["is_custom"],
        sort_order=row["sort_order"],
    )


# ─── reads ──────────────────────────────────────────────────────────────


def list_activity_types(user_id: str) -> list[ActivityType]:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT *
              FROM lifelog_activity_types
             WHERE user_id = %s
             ORDER BY is_pinned DESC, sort_order, label
            """,
            [user_id],
        )
        rows = cur.fetchall()
    return [_row_to_schema(r) for r in rows]


def get_activity_type(user_id: str, slug: str) -> ActivityType | None:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT * FROM lifelog_activity_types
             WHERE user_id = %s AND slug = %s
            """,
            [user_id, slug],
        )
        row = cur.fetchone()
    return _row_to_schema(row) if row else None


# ─── writes ─────────────────────────────────────────────────────────────


class ActivityTypeError(Exception):
    """Domain error for activity-type CRUD. Route layer maps to HTTP
    status. We use exceptions (not Result types) so the call sites stay
    flat — there's only ~4 of them."""


class SlugTakenError(ActivityTypeError):
    pass


class NotFoundError(ActivityTypeError):
    pass


# NOTE: SystemSeedError used to fence off deletion of the 8 default
# activities. Removed in favor of "any activity is deletable" — the
# seeds are just convenience defaults, not protected rows. The
# `is_custom` column stays as metadata (used for analytics + telling
# users "these are the starter set") but no longer gates DELETE.


def create_activity_type(
    user_id: str, req: CreateActivityTypeRequest
) -> ActivityType:
    slug = _slug_from_label(req.label)
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        # Check for slug collision before insert — cleaner error than the
        # UNIQUE constraint blowing up.
        cur.execute(
            "SELECT 1 FROM lifelog_activity_types WHERE user_id = %s AND slug = %s",
            [user_id, slug],
        )
        if cur.fetchone() is not None:
            raise SlugTakenError(slug)

        cur.execute(
            """
            INSERT INTO lifelog_activity_types (
              user_id, slug, label, emoji, hue, kind, focus_mode,
              default_capture_location, default_capture_contacts,
              live_activity_show_timer, sort_order, is_pinned, is_custom
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING *
            """,
            [
                user_id, slug, req.label, req.emoji, req.hue, req.kind,
                req.focus_mode, req.default_capture_location,
                req.default_capture_contacts, req.live_activity_show_timer,
                req.sort_order, req.is_pinned,
            ],
        )
        row = cur.fetchone()
        assert row is not None
    return _row_to_schema(row)


def update_activity_type(
    user_id: str, slug: str, req: UpdateActivityTypeRequest
) -> ActivityType:
    # Build SET clause from non-None fields only. Pydantic .model_dump
    # with exclude_none is the cleanest way.
    diff = req.model_dump(exclude_none=True)
    if not diff:
        # No-op edit. Just return the current row.
        current = get_activity_type(user_id, slug)
        if current is None:
            raise NotFoundError(slug)
        return current

    # Reject keys not in the safelist (defensive — Pydantic already
    # validated, but this keeps SQL safe against future schema drift).
    safe_diff = {k: v for k, v in diff.items() if k in _UPDATABLE_COLUMNS}
    if not safe_diff:
        current = get_activity_type(user_id, slug)
        if current is None:
            raise NotFoundError(slug)
        return current

    set_clauses = [f"{col} = %s" for col in safe_diff]
    set_clauses.append("updated_at = now()")
    params: list = list(safe_diff.values())
    params.extend([user_id, slug])

    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            UPDATE lifelog_activity_types
               SET {", ".join(set_clauses)}
             WHERE user_id = %s AND slug = %s
             RETURNING *
            """,
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise NotFoundError(slug)
    return _row_to_schema(row)


def delete_activity_type(user_id: str, slug: str) -> None:
    """Delete any activity, including the seeded defaults. Logged events
    keep their `activity_type` reference (it's a string slug, not a FK)
    so history doesn't break — events for a deleted activity will just
    fall back to the activity_type slug as their title at render time."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            DELETE FROM lifelog_activity_types
             WHERE user_id = %s AND slug = %s
             RETURNING id
            """,
            [user_id, slug],
        )
        if cur.fetchone() is None:
            raise NotFoundError(slug)


# ─── back-compat helpers (used by service.start_event for title fallback) ─


def reload_activity_types() -> int:
    """Compatibility shim — the old JSON-cache had a manual reload. With
    DB-backed types every read goes to the DB, so reload is a no-op.
    Returns the current count for the iOS Settings reload-button UX."""
    # We don't know the user here. The route layer fills this in.
    return 0
