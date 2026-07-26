"""Shared helpers for the unification layer."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings
from unify import taxonomy

log = get_logger(__name__)

LOCAL_TZ = ZoneInfo(getattr(settings, "LOCAL_TZ", None) or "America/New_York")

LB_PER_KG = 2.2046226218
KG_PER_LB = 0.45359237


def local_day(ts: datetime | None) -> date | None:
    """The America/New_York calendar date a timestamp falls on."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=LOCAL_TZ)
    return ts.astimezone(LOCAL_TZ).date()


def uid(source: str, *parts) -> str:
    """Deterministic activity/sleep/entry uid."""
    tail = ":".join(str(p) for p in parts if p is not None)
    return f"{source}:{tail}"


def hash_uid(source: str, *parts) -> str:
    """Deterministic uid for sources with no stable id of their own (CSV rows,
    PDF lines). Hashing the content keeps re-imports idempotent."""
    blob = "|".join("" if p is None else str(p) for p in parts)
    return f"{source}:{hashlib.sha1(blob.encode()).hexdigest()[:20]}"


def g_to_kg(grams) -> float | None:
    return None if grams in (None, "") else float(grams) / 1000.0


def lb_to_kg(lb) -> float | None:
    return None if lb in (None, "") else float(lb) * KG_PER_LB


def ms_to_s(ms) -> float | None:
    return None if ms in (None, "") else float(ms) / 1000.0


def num(v):
    """Best-effort numeric coercion. Empty/garbage -> None."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def jsonb(v) -> Jsonb:
    return Jsonb(v if v is not None else {})


# ---------------------------------------------------------------------------
# Whoop CSV-export timestamps
#
# The export writes naive local wall-clock strings and puts the real offset in
# a separate `Cycle timezone` column ("UTC-04:00"). Reconstructing the instant
# from the pair matters -- several of these rows were recorded abroad, so
# assuming America/New_York would shift them by hours.
# ---------------------------------------------------------------------------

_TZ_RE = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")


def parse_offset(tz_str: str | None) -> timezone:
    m = _TZ_RE.match((tz_str or "").strip())
    if not m:
        return UTC
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def parse_ts(value: str | None, tz: timezone) -> datetime | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        try:
            return datetime.fromisoformat(v).replace(tzinfo=tz)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Dimension seeding
# ---------------------------------------------------------------------------

def seed_dimensions(conn: psycopg.Connection) -> dict[str, int]:
    """Write the canonical movement catalogue and the vendor activity-type map
    into dim_exercise / dim_activity_type. Idempotent."""
    n_ex = n_at = 0
    with conn.cursor() as cur:
        for m in taxonomy.MOVEMENTS:
            cur.execute(
                """
                INSERT INTO dim_exercise (exercise_key, display_name, movement_pattern,
                    primary_muscle, equipment, is_compound, is_spinal_flexion, is_unilateral)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (exercise_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    movement_pattern = EXCLUDED.movement_pattern,
                    primary_muscle = EXCLUDED.primary_muscle,
                    equipment = EXCLUDED.equipment,
                    is_compound = EXCLUDED.is_compound,
                    is_spinal_flexion = EXCLUDED.is_spinal_flexion,
                    is_unilateral = EXCLUDED.is_unilateral,
                    updated_at = now()
                """,
                [m.key, m.display, m.pattern, m.muscle, m.equipment,
                 m.compound, m.spinal_flexion, m.unilateral],
            )
            n_ex += 1

        for source, table in taxonomy.ACTIVITY_TYPE_MAP.items():
            for vendor_type in table:
                ctype, is_res, is_cardio = taxonomy.canonical_activity_type(source, vendor_type)
                cur.execute(
                    """
                    INSERT INTO dim_activity_type
                        (source, vendor_type, canonical_type, is_resistance, is_cardio)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (source, vendor_type) DO UPDATE SET
                        canonical_type = EXCLUDED.canonical_type,
                        is_resistance = EXCLUDED.is_resistance,
                        is_cardio = EXCLUDED.is_cardio,
                        updated_at = now()
                    """,
                    [source, vendor_type, ctype, is_res, is_cardio],
                )
                n_at += 1
    return {"exercises": n_ex, "activity_types": n_at}


def register_exercise(conn: psycopg.Connection, source: str, vendor_key: str,
                      vendor_label: str, exercise_key: str, known: bool) -> None:
    """Record a vendor->canonical mapping, creating a placeholder dim_exercise
    row for auto-derived keys so the FK on dim_exercise_alias holds."""
    with conn.cursor() as cur:
        if not known:
            cur.execute(
                """
                INSERT INTO dim_exercise (exercise_key, display_name, notes)
                VALUES (%s, %s, 'auto-derived from vendor label; needs review')
                ON CONFLICT (exercise_key) DO NOTHING
                """,
                [exercise_key, vendor_label or exercise_key],
            )
        cur.execute(
            """
            INSERT INTO dim_exercise_alias
                (source, vendor_key, exercise_key, vendor_label, confidence)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (source, vendor_key) DO UPDATE SET
                exercise_key = EXCLUDED.exercise_key,
                vendor_label = EXCLUDED.vendor_label,
                confidence = EXCLUDED.confidence,
                updated_at = now()
            """,
            [source, vendor_key, exercise_key, vendor_label, 1.0 if known else 0.4],
        )


def resolve_and_register(conn: psycopg.Connection, source: str, vendor_label: str,
                         vendor_id: str | None = None,
                         vendor_category: str | None = None) -> str:
    key, known = taxonomy.resolve_exercise(vendor_label, vendor_id, vendor_category)
    register_exercise(conn, source, vendor_id or taxonomy.normalize_label(vendor_label),
                      vendor_label, key, known)
    return key


# ---------------------------------------------------------------------------
# Bulk upsert
# ---------------------------------------------------------------------------

def upsert(conn: psycopg.Connection, table: str, rows: list[dict],
           conflict: list[str], update: list[str] | None = None) -> int:
    """Batched INSERT ... ON CONFLICT DO UPDATE. Returns rows written."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if update is None:
        update = [c for c in cols if c not in conflict]
    collist = ", ".join(f'"{c}"' for c in cols)
    vals = ", ".join(["%s"] * len(cols))
    conflict_list = ", ".join(f'"{c}"' for c in conflict)
    setlist = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update)
    sql = (
        f'INSERT INTO "{table}" ({collist}) VALUES ({vals}) '
        f"ON CONFLICT ({conflict_list}) DO UPDATE SET {setlist}"
    )
    written = 0
    with conn.cursor() as cur:
        for chunk_start in range(0, len(rows), 500):
            chunk = rows[chunk_start:chunk_start + 500]
            cur.executemany(sql, [[r.get(c) for c in cols] for r in chunk])
            written += len(chunk)
    return written
