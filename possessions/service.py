"""Every possession read and write, in one place.

The MCP tools, the HTTP routes and the ingest CLI all call in here rather than
writing their own SQL, so a change to how "purge candidate" is defined lands
in one place and the three surfaces can't disagree about it.

Writes go through `_audit` (lifeos_core.audit) with the statement and params,
same as the generic db_* tools. Reads don't — auditing a SELECT would bury the
mutations you actually want to find.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from lifeos_core.audit import record_write
from lifeos_core.db import conn, tx
from lifeos_core.logging import get_logger

from . import vocab

log = get_logger(__name__)

MAX_LIMIT = 500

# Columns a caller may set through log_possession / update_possession. Anything
# outside this set is rejected rather than ignored: silently dropping a field
# the caller believed it wrote is the kind of bug that surfaces a month later
# as "why is half my inventory missing sizes".
WRITABLE_COLUMNS: frozenset[str] = frozenset({
    "name", "kind", "subcategory", "brand", "colorway", "primary_color",
    "secondary_color", "pattern", "material", "size", "formality", "warmth",
    "weather_tags", "packed_volume_l", "weight_g", "acquired_on", "cost_cents",
    "retailer", "condition", "status", "location_id", "photo_original",
    "photo_cutout", "photo_thumb", "photo_hash", "tag_confidence", "tag_model",
    "reviewed", "notes",
})

_SUMMARY_COLUMNS = """
  s.id, s.name, s.kind::text AS kind, s.subcategory, s.brand, s.primary_color,
  s.status::text AS status, s.condition, s.reviewed, s.acquired_on,
  s.cost_cents, s.location_path, s.wear_count_30, s.wear_count_90,
  s.wear_count_365, s.wear_count_all, s.last_worn_on, s.days_since_worn,
  s.cost_per_wear_cents, s.is_stale
"""


class PossessionError(ValueError):
    """A caller-fixable problem: unknown location, bad vocab, missing item."""


# --------------------------------------------------------------- helpers ----

def _rows(sql: str, params: list | None = None) -> list[dict]:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()


def _clamp(limit: int | None, default: int = 100) -> int:
    if not limit or limit <= 0:
        return default
    return min(int(limit), MAX_LIMIT)


def _as_uuids(ids: list[str] | list[UUID]) -> list[UUID]:
    out: list[UUID] = []
    for raw in ids:
        try:
            out.append(raw if isinstance(raw, UUID) else UUID(str(raw)))
        except (ValueError, AttributeError) as e:
            raise PossessionError(f"not a possession id: {raw!r}") from e
    if not out:
        raise PossessionError("no possession ids given")
    return out


def resolve_location(name: str, *, required: bool = True) -> UUID | None:
    """Locations are addressed by name everywhere a human is involved — 'move
    these to Bin A', not 'move these to 3f2a…'. Matching is case-insensitive
    and also accepts the full path's last segment."""
    if not name:
        if required:
            raise PossessionError("location name is required")
        return None
    rows = _rows(
        "SELECT id, name FROM dim_storage_location WHERE lower(name) = lower(%s)",
        [name.strip()],
    )
    if not rows:
        rows = _rows(
            "SELECT id, name FROM dim_storage_location "
            "WHERE lower(name) LIKE lower(%s) ORDER BY length(name) LIMIT 2",
            [f"%{name.strip()}%"],
        )
    if not rows:
        if required:
            known = [r["name"] for r in _rows(
                "SELECT name FROM dim_storage_location ORDER BY name LIMIT 25")]
            raise PossessionError(
                f"no storage location matching {name!r}. "
                f"Known: {', '.join(known) if known else '(none yet — create one first)'}"
            )
        return None
    if len(rows) > 1:
        raise PossessionError(
            f"{name!r} matches more than one location "
            f"({', '.join(r['name'] for r in rows)}); use the full name"
        )
    return rows[0]["id"]


# ----------------------------------------------------------------- reads ----

def get_possessions(
    kind: str | None = None,
    status: str | None = "active",
    location: str | None = None,
    primary_color: str | None = None,
    unworn_since: int | None = None,
    reviewed: bool | None = None,
    search: str | None = None,
    weather_tags: list[str] | None = None,
    limit: int | None = 100,
) -> list[dict]:
    """Filtered inventory, newest-acquired first.

    `unworn_since` is a day count: 180 means "not worn in the last 180 days",
    and an item never worn at all counts as unworn (that is the point of the
    filter — a never-worn item is the strongest purge signal there is).
    """
    where: list[str] = []
    params: list[Any] = []

    if kind:
        if kind not in vocab.KINDS:
            raise PossessionError(f"unknown kind {kind!r}; one of {', '.join(vocab.KINDS)}")
        where.append("s.kind = %s::possession_kind")
        params.append(kind)
    if status:
        if status not in vocab.STATUSES:
            raise PossessionError(
                f"unknown status {status!r}; one of {', '.join(vocab.STATUSES)}")
        where.append("s.status = %s::possession_status")
        params.append(status)
    if location:
        where.append("s.location_id = %s")
        params.append(resolve_location(location))
    if primary_color:
        where.append("s.primary_color = %s")
        params.append(primary_color.strip().lower())
    if unworn_since is not None:
        where.append("(s.last_worn_on IS NULL OR s.last_worn_on < CURRENT_DATE - %s)")
        params.append(int(unworn_since))
    if reviewed is not None:
        where.append("s.reviewed = %s")
        params.append(bool(reviewed))
    if search:
        where.append("(s.name ILIKE %s OR s.brand ILIKE %s OR s.subcategory ILIKE %s)")
        params.extend([f"%{search}%"] * 3)
    if weather_tags:
        bad = [t for t in weather_tags if t not in vocab.WEATHER_TAGS]
        if bad:
            raise PossessionError(
                f"unknown weather tag(s) {bad}; one of {', '.join(vocab.WEATHER_TAGS)}")
        # Overlap, not containment: "what works in rain OR cold" is the
        # question a packing list asks.
        where.append("p.weather_tags && %s")
        params.append(list(weather_tags))

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(_clamp(limit))
    return _rows(
        f"""
        SELECT {_SUMMARY_COLUMNS}, p.weather_tags, p.photo_thumb, p.photo_cutout
          FROM mart_possession_summary s
          JOIN dim_possession p ON p.id = s.id
        {clause}
         ORDER BY s.acquired_on DESC NULLS LAST, s.name
         LIMIT %s
        """,
        params,
    )


def get_possession(possession_id: str | UUID) -> dict | None:
    """One item, fully hydrated: every column, its wear history, and the
    outfits and trips it appears in."""
    pid = _as_uuids([possession_id])[0]
    rows = _rows(
        f"""
        SELECT {_SUMMARY_COLUMNS},
               p.colorway, p.secondary_color, p.pattern, p.material, p.size,
               p.formality, p.warmth, p.weather_tags, p.packed_volume_l,
               p.weight_g, p.retailer, p.location_id, p.photo_original,
               p.photo_cutout, p.photo_thumb, p.photo_hash, p.tag_confidence,
               p.tag_model, p.notes, p.created_at, p.updated_at
          FROM mart_possession_summary s
          JOIN dim_possession p ON p.id = s.id
         WHERE s.id = %s
        """,
        [pid],
    )
    if not rows:
        return None
    item = rows[0]
    item["wear_history"] = _rows(
        "SELECT worn_on, context, city FROM fact_wear "
        "WHERE possession_id = %s ORDER BY worn_on DESC LIMIT 200",
        [pid],
    )
    item["outfits"] = _rows(
        "SELECT o.id, o.name, o.formality FROM dim_outfit o "
        "JOIN bridge_outfit_possession b ON b.outfit_id = o.id "
        "WHERE b.possession_id = %s ORDER BY o.name",
        [pid],
    )
    item["trips"] = _rows(
        "SELECT t.id, t.name, t.start_date, t.end_date, b.state FROM dim_trip t "
        "JOIN bridge_trip_packing b ON b.trip_id = t.id "
        "WHERE b.possession_id = %s ORDER BY t.start_date DESC",
        [pid],
    )
    return item


def get_storage_contents(location_name: str, include_nested: bool = True) -> dict:
    """What is in this container. Nested by default — asking about "Under bed"
    when the items are in "Bin A" inside it and getting nothing back is a
    wrong answer dressed as an empty one."""
    loc_id = resolve_location(location_name)
    location = _rows(
        "SELECT id, name, kind, full_path, depth FROM vw_storage_location WHERE id = %s",
        [loc_id],
    )[0]

    if include_nested:
        items = _rows(
            f"""
            WITH RECURSIVE subtree AS (
              SELECT id FROM dim_storage_location WHERE id = %s
              UNION ALL
              SELECT c.id FROM dim_storage_location c
                JOIN subtree st ON c.parent_id = st.id
            )
            SELECT {_SUMMARY_COLUMNS}, p.photo_thumb
              FROM mart_possession_summary s
              JOIN dim_possession p ON p.id = s.id
             WHERE s.location_id IN (SELECT id FROM subtree)
             ORDER BY s.kind, s.name
            """,
            [loc_id],
        )
    else:
        items = _rows(
            f"""
            SELECT {_SUMMARY_COLUMNS}, p.photo_thumb
              FROM mart_possession_summary s
              JOIN dim_possession p ON p.id = s.id
             WHERE s.location_id = %s
             ORDER BY s.kind, s.name
            """,
            [loc_id],
        )

    children = _rows(
        "SELECT id, name, kind, full_path, active_item_count FROM vw_storage_location "
        "WHERE parent_id = %s ORDER BY name",
        [loc_id],
    )
    return {"location": location, "children": children,
            "items": items, "item_count": len(items)}


def list_locations() -> list[dict]:
    return _rows(
        "SELECT id, name, kind, parent_id, full_path, depth, active_item_count "
        "FROM vw_storage_location ORDER BY full_path"
    )


def get_purge_candidates(min_days_unworn: int = 180, limit: int = 50) -> list[dict]:
    """Still-owned things you haven't touched, worst first.

    Ordered by days unworn desc, then cost-per-wear desc: the second key is
    what separates "an old t-shirt" from "a £400 jacket worn twice", and the
    second one is the decision actually worth making. Never-worn items sort
    to the top via their acquisition date.
    """
    return _rows(
        f"""
        SELECT {_SUMMARY_COLUMNS}, p.photo_thumb,
               COALESCE(s.days_since_worn,
                        (CURRENT_DATE - s.acquired_on)::int) AS days_idle
          FROM mart_possession_summary s
          JOIN dim_possession p ON p.id = s.id
         WHERE s.status = 'active'
           AND (s.last_worn_on IS NULL OR s.last_worn_on <= CURRENT_DATE - %s)
           AND (s.acquired_on IS NULL OR s.acquired_on <= CURRENT_DATE - %s)
         ORDER BY days_idle DESC NULLS FIRST,
                  s.cost_per_wear_cents DESC NULLS LAST,
                  s.cost_cents DESC NULLS LAST
         LIMIT %s
        """,
        [int(min_days_unworn), int(min_days_unworn), _clamp(limit, 50)],
    )


def get_outfits(formality: int | None = None,
                weather_tags: list[str] | None = None) -> list[dict]:
    where, params = [], []
    if formality is not None:
        where.append("o.formality = %s")
        params.append(int(formality))
    if weather_tags:
        where.append("o.weather_tags && %s")
        params.append(list(weather_tags))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return _rows(
        f"""
        SELECT o.id, o.name, o.formality, o.weather_tags, o.notes,
               COALESCE(jsonb_agg(
                 jsonb_build_object('id', p.id, 'name', p.name,
                                    'kind', p.kind, 'photo_thumb', p.photo_thumb)
                 ORDER BY p.kind
               ) FILTER (WHERE p.id IS NOT NULL), '[]'::jsonb) AS items
          FROM dim_outfit o
          LEFT JOIN bridge_outfit_possession b ON b.outfit_id = o.id
          LEFT JOIN dim_possession p ON p.id = b.possession_id
        {clause}
         GROUP BY o.id
         ORDER BY o.formality NULLS LAST, o.name
        """,
        params,
    )


def list_trips(upcoming_only: bool = False) -> list[dict]:
    clause = "WHERE t.end_date >= CURRENT_DATE" if upcoming_only else ""
    return _rows(
        f"""
        SELECT t.id, t.name, t.start_date, t.end_date, t.destinations, t.notes,
               (t.end_date - t.start_date + 1) AS nights,
               count(b.possession_id)                                  AS candidate_count,
               count(b.possession_id) FILTER (WHERE b.state = 'packed') AS packed_count
          FROM dim_trip t
          LEFT JOIN bridge_trip_packing b ON b.trip_id = t.id
        {clause}
         GROUP BY t.id
         ORDER BY t.start_date DESC
        """
    )


def get_trip(trip_id: str | UUID) -> dict | None:
    tid = _as_uuids([trip_id])[0]
    rows = _rows("SELECT * FROM dim_trip WHERE id = %s", [tid])
    if not rows:
        return None
    trip = rows[0]
    trip["packing"] = _rows(
        f"""
        SELECT {_SUMMARY_COLUMNS}, p.photo_thumb, p.weather_tags, p.warmth,
               p.formality, b.qty, b.state, b.slot, b.notes AS packing_notes
          FROM bridge_trip_packing b
          JOIN mart_possession_summary s ON s.id = b.possession_id
          JOIN dim_possession p ON p.id = s.id
         WHERE b.trip_id = %s
         ORDER BY s.kind, s.name
        """,
        [tid],
    )
    return trip


def review_queue(limit: int = 50) -> list[dict]:
    """Unreviewed items, oldest capture first so the batch drains in the order
    it was shot."""
    return _rows(
        """
        SELECT p.*, l.full_path AS location_path
          FROM dim_possession p
          LEFT JOIN vw_storage_location l ON l.id = p.location_id
         WHERE p.reviewed = FALSE
         ORDER BY p.created_at
         LIMIT %s
        """,
        [_clamp(limit, 50)],
    )


def stats() -> dict:
    return _rows(
        """
        SELECT count(*)                                             AS total,
               count(*) FILTER (WHERE status = 'active')            AS active,
               count(*) FILTER (WHERE reviewed = FALSE)             AS unreviewed,
               count(*) FILTER (WHERE status = 'purge_candidate')   AS purge_candidates,
               count(*) FILTER (WHERE photo_cutout IS NOT NULL)     AS with_cutout,
               count(*) FILTER (WHERE location_id IS NULL
                                AND status = 'active')              AS unlocated,
               sum(cost_cents) FILTER (WHERE status = 'active')     AS active_cost_cents
          FROM dim_possession
        """
    )[0]


# ---------------------------------------------------------------- writes ----

def _coerce_writes(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and adapt a caller's field map before it reaches SQL."""
    unknown = set(patch) - WRITABLE_COLUMNS
    if unknown:
        raise PossessionError(
            f"not writable: {', '.join(sorted(unknown))}. "
            f"Writable: {', '.join(sorted(WRITABLE_COLUMNS))}"
        )
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "kind" and value is not None and value not in vocab.KINDS:
            raise PossessionError(f"unknown kind {value!r}")
        if key == "status" and value is not None and value not in vocab.STATUSES:
            raise PossessionError(f"unknown status {value!r}")
        if key in ("primary_color", "secondary_color") and value is not None:
            normalized = vocab.normalize_color(value)
            if normalized is None:
                raise PossessionError(
                    f"{value!r} is not in the colour vocabulary. "
                    f"Legal values: {', '.join(vocab.colors())}"
                )
            value = normalized
        if key == "weather_tags" and value is not None:
            bad = [t for t in value if t not in vocab.WEATHER_TAGS]
            if bad:
                raise PossessionError(f"unknown weather tag(s) {bad}")
            value = sorted(set(value))
        if key == "tag_confidence" and isinstance(value, dict):
            value = Jsonb(value)
        out[key] = value
    return out


def _check_subcategory(kind: str | None, subcategory: str | None) -> None:
    """The composite FK enforces this too, but its error message is a
    constraint name. Fail here with the legal list instead."""
    if subcategory is None or kind is None:
        return
    if not vocab.is_valid_subcategory(kind, subcategory):
        legal = vocab.subcategories().get(kind, [])
        raise PossessionError(
            f"{subcategory!r} is not a subcategory of {kind!r}. "
            f"Legal: {', '.join(legal) if legal else '(none defined for this kind)'}"
        )


def log_possession(**fields: Any) -> dict:
    """Create one possession. Idempotent on photo_hash when given: re-running
    the ingest over the same photo updates the existing row instead of adding
    a twin."""
    for required in ("name", "kind", "primary_color"):
        if not fields.get(required):
            raise PossessionError(f"{required} is required")

    location_name = fields.pop("location", None)
    if location_name:
        fields["location_id"] = resolve_location(location_name)

    _check_subcategory(fields.get("kind"), fields.get("subcategory"))
    values = _coerce_writes(fields)

    cols = list(values)
    placeholders = ", ".join(["%s"] * len(cols))
    updatable = [c for c in cols if c != "photo_hash"]
    on_conflict = (
        "ON CONFLICT (photo_hash) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        if "photo_hash" in cols and values.get("photo_hash")
        else "ON CONFLICT DO NOTHING"
    )
    sql = (
        f"INSERT INTO dim_possession ({', '.join(cols)}) VALUES ({placeholders}) "
        f"{on_conflict} RETURNING id, name, kind::text AS kind, status::text AS status, "
        f"(xmax = 0) AS inserted"
    )
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [values[k] for k in cols])
        row = cur.fetchone()

    record_write(tool="log_possession", operation="INSERT",
                 target_table="dim_possession", statement=sql,
                 params={k: str(v) for k, v in values.items()},
                 affected_rows=1 if row else 0, result_sample=[row] if row else [])
    if not row:
        raise PossessionError("insert was a no-op (duplicate with no photo_hash to merge on)")
    return row


def update_possession(possession_id: str | UUID, patch: dict[str, Any]) -> dict:
    pid = _as_uuids([possession_id])[0]
    if not patch:
        raise PossessionError("empty patch")

    location_name = patch.pop("location", None)
    if location_name is not None:
        patch["location_id"] = resolve_location(location_name) if location_name else None

    # Subcategory is validated against the row's kind, which the patch may or
    # may not be changing — read the current one when it isn't.
    if "subcategory" in patch:
        kind = patch.get("kind")
        if kind is None:
            existing = _rows("SELECT kind::text AS kind FROM dim_possession WHERE id = %s",
                             [pid])
            if not existing:
                raise PossessionError(f"no possession {possession_id}")
            kind = existing[0]["kind"]
        _check_subcategory(kind, patch["subcategory"])

    values = _coerce_writes(patch)
    assignments = ", ".join(f"{c} = %s" for c in values)
    sql = (f"UPDATE dim_possession SET {assignments} WHERE id = %s "
           f"RETURNING id, name, kind::text AS kind, status::text AS status, reviewed")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [*values.values(), pid])
        row = cur.fetchone()
        affected = cur.rowcount

    record_write(tool="update_possession", operation="UPDATE",
                 target_table="dim_possession", statement=sql,
                 params={k: str(v) for k, v in values.items()} | {"id": str(pid)},
                 affected_rows=affected, result_sample=[row] if row else [])
    if not row:
        raise PossessionError(f"no possession {possession_id}")
    return row


def set_possession_status(ids: list[str], status: str) -> dict:
    if status not in vocab.STATUSES:
        raise PossessionError(
            f"unknown status {status!r}; one of {', '.join(vocab.STATUSES)}")
    pids = _as_uuids(ids)
    sql = ("UPDATE dim_possession SET status = %s::possession_status "
           "WHERE id = ANY(%s) RETURNING id, name, status::text AS status")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [status, pids])
        rows = cur.fetchall()

    record_write(tool="set_possession_status", operation="UPDATE",
                 target_table="dim_possession", statement=sql,
                 params={"status": status, "ids": [str(p) for p in pids]},
                 affected_rows=len(rows), result_sample=rows[:20])
    return {"updated": len(rows), "status": status, "items": rows}


def move_possession(ids: list[str], location_name: str) -> dict:
    pids = _as_uuids(ids)
    loc_id = resolve_location(location_name)
    sql = ("UPDATE dim_possession SET location_id = %s WHERE id = ANY(%s) "
           "RETURNING id, name")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [loc_id, pids])
        rows = cur.fetchall()

    record_write(tool="move_possession", operation="UPDATE",
                 target_table="dim_possession", statement=sql,
                 params={"location": location_name, "ids": [str(p) for p in pids]},
                 affected_rows=len(rows), result_sample=rows[:20])
    return {"moved": len(rows), "location": location_name, "items": rows}


def log_wear(possession_ids: list[str], worn_on: date | str | None = None,
             context: str | None = None, city: str | None = None) -> dict:
    """Record that these items were worn. Safe to call twice — the natural key
    (item, day, context) absorbs the retry."""
    pids = _as_uuids(possession_ids)
    day = worn_on or date.today()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    sql = ("INSERT INTO fact_wear (possession_id, worn_on, context, city) "
           "SELECT unnest(%s::uuid[]), %s, %s, %s "
           "ON CONFLICT (possession_id, worn_on, context) DO NOTHING "
           "RETURNING possession_id")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [pids, day, context, city])
        rows = cur.fetchall()

    record_write(tool="log_wear", operation="INSERT", target_table="fact_wear",
                 statement=sql,
                 params={"ids": [str(p) for p in pids], "worn_on": str(day),
                         "context": context, "city": city},
                 affected_rows=len(rows), result_sample=rows[:20])
    return {"logged": len(rows), "already_logged": len(pids) - len(rows),
            "worn_on": str(day), "context": context, "city": city}


def create_location(name: str, kind: str, parent: str | None = None,
                    notes: str | None = None) -> dict:
    parent_id = resolve_location(parent) if parent else None
    sql = ("INSERT INTO dim_storage_location (name, kind, parent_id, notes) "
           "VALUES (%s, %s, %s, %s) "
           "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, "
           "parent_id = EXCLUDED.parent_id, notes = EXCLUDED.notes "
           "RETURNING id, name, kind")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [name.strip(), kind, parent_id, notes])
        row = cur.fetchone()

    record_write(tool="create_location", operation="INSERT",
                 target_table="dim_storage_location", statement=sql,
                 params={"name": name, "kind": kind, "parent": parent},
                 affected_rows=1, result_sample=[row])
    return row


def create_outfit(name: str, possession_ids: list[str], formality: int | None = None,
                  weather_tags: list[str] | None = None,
                  notes: str | None = None) -> dict:
    pids = _as_uuids(possession_ids)
    if weather_tags:
        bad = [t for t in weather_tags if t not in vocab.WEATHER_TAGS]
        if bad:
            raise PossessionError(f"unknown weather tag(s) {bad}")
    sql = ("INSERT INTO dim_outfit (name, formality, weather_tags, notes) "
           "VALUES (%s, %s, %s, %s) "
           "ON CONFLICT (name) DO UPDATE SET formality = EXCLUDED.formality, "
           "weather_tags = EXCLUDED.weather_tags, notes = EXCLUDED.notes "
           "RETURNING id, name")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [name.strip(), formality, sorted(set(weather_tags or [])), notes])
        outfit = cur.fetchone()
        cur.execute("DELETE FROM bridge_outfit_possession WHERE outfit_id = %s",
                    [outfit["id"]])
        cur.execute(
            "INSERT INTO bridge_outfit_possession (outfit_id, possession_id) "
            "SELECT %s, unnest(%s::uuid[])",
            [outfit["id"], pids],
        )

    record_write(tool="create_outfit", operation="INSERT", target_table="dim_outfit",
                 statement=sql,
                 params={"name": name, "items": [str(p) for p in pids]},
                 affected_rows=1, result_sample=[outfit])
    return {"id": outfit["id"], "name": outfit["name"], "item_count": len(pids)}


def create_trip(name: str, start_date: date | str, end_date: date | str,
                destinations: list[dict] | None = None,
                notes: str | None = None) -> dict:
    start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    end = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    if end < start:
        raise PossessionError("end_date is before start_date")
    sql = ("INSERT INTO dim_trip (name, start_date, end_date, destinations, notes) "
           "VALUES (%s, %s, %s, %s::jsonb, %s) "
           "ON CONFLICT (name) DO UPDATE SET start_date = EXCLUDED.start_date, "
           "end_date = EXCLUDED.end_date, destinations = EXCLUDED.destinations, "
           "notes = EXCLUDED.notes "
           "RETURNING id, name, start_date, end_date, destinations")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [name.strip(), start, end,
                          json.dumps(destinations or [], default=str), notes])
        row = cur.fetchone()

    record_write(tool="create_trip", operation="INSERT", target_table="dim_trip",
                 statement=sql, params={"name": name, "start": str(start),
                                        "end": str(end)},
                 affected_rows=1, result_sample=[row])
    return row


def set_packing_state(trip_id: str | UUID, possession_id: str | UUID,
                      state: str, qty: int = 1, slot: str | None = None) -> dict:
    if state not in vocab.PACKING_STATES:
        raise PossessionError(
            f"unknown packing state {state!r}; one of {', '.join(vocab.PACKING_STATES)}")
    tid = _as_uuids([trip_id])[0]
    pid = _as_uuids([possession_id])[0]
    sql = ("INSERT INTO bridge_trip_packing (trip_id, possession_id, qty, state, slot) "
           "VALUES (%s, %s, %s, %s, %s) "
           "ON CONFLICT (trip_id, possession_id) DO UPDATE SET "
           "state = EXCLUDED.state, qty = EXCLUDED.qty, "
           "slot = COALESCE(EXCLUDED.slot, bridge_trip_packing.slot), "
           "updated_at = now() "
           "RETURNING trip_id, possession_id, qty, state, slot")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [tid, pid, max(1, int(qty)), state, slot])
        row = cur.fetchone()

    record_write(tool="set_packing_state", operation="UPSERT",
                 target_table="bridge_trip_packing", statement=sql,
                 params={"trip": str(tid), "possession": str(pid), "state": state},
                 affected_rows=1, result_sample=[row])
    return row


def save_packing_candidates(trip_id: str | UUID,
                            selections: list[dict]) -> int:
    """Persist a generated packing list as candidates, preserving any state a
    human already set. Someone who has ticked three things as packed and then
    regenerates the list should not have to tick them again."""
    tid = _as_uuids([trip_id])[0]
    if not selections:
        return 0
    sql = ("INSERT INTO bridge_trip_packing (trip_id, possession_id, qty, state, slot) "
           "VALUES (%s, %s, %s, 'candidate', %s) "
           "ON CONFLICT (trip_id, possession_id) DO UPDATE SET "
           "qty = EXCLUDED.qty, slot = EXCLUDED.slot, updated_at = now()")
    with tx() as c, c.cursor() as cur:
        for sel in selections:
            cur.execute(sql, [tid, _as_uuids([sel["id"]])[0],
                              max(1, int(sel.get("qty", 1))), sel.get("slot")])
    record_write(tool="build_packing_list", operation="UPSERT",
                 target_table="bridge_trip_packing", statement=sql,
                 params={"trip": str(tid), "count": len(selections)},
                 affected_rows=len(selections))
    return len(selections)


def add_color(name: str, family: str, hex_code: str | None = None) -> dict:
    """Extend the colour vocabulary. The right move when the tagger keeps
    reaching for a colour that isn't there — loosening the FK is not."""
    sql = ("INSERT INTO dim_color (name, family, hex) VALUES (%s, %s, %s) "
           "ON CONFLICT (name) DO UPDATE SET family = EXCLUDED.family, "
           "hex = EXCLUDED.hex RETURNING name, family, hex")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [name.strip().lower(), family.strip().lower(), hex_code])
        row = cur.fetchone()
    vocab.invalidate()
    record_write(tool="add_color", operation="INSERT", target_table="dim_color",
                 statement=sql, params={"name": name}, affected_rows=1,
                 result_sample=[row])
    return row


def add_subcategory(kind: str, name: str, notes: str | None = None) -> dict:
    if kind not in vocab.KINDS:
        raise PossessionError(f"unknown kind {kind!r}")
    sql = ("INSERT INTO dim_possession_subcategory (kind, name, notes) "
           "VALUES (%s::possession_kind, %s, %s) "
           "ON CONFLICT (kind, name) DO UPDATE SET notes = EXCLUDED.notes "
           "RETURNING kind::text AS kind, name")
    with tx() as c, c.cursor() as cur:
        cur.execute(sql, [kind, name.strip().lower(), notes])
        row = cur.fetchone()
    vocab.invalidate()
    record_write(tool="add_subcategory", operation="INSERT",
                 target_table="dim_possession_subcategory", statement=sql,
                 params={"kind": kind, "name": name}, affected_rows=1,
                 result_sample=[row])
    return row
