"""Cal AI ingestion: Firestore diary -> raw_calai_food -> fact_food_log/daily.

The DB side here is final and tested-shaped: it upserts the raw diary doc, maps
each entry's food object onto fact_food_log (source='calai'), and recomputes
fact_food_daily for the touched days. Idempotent (keyed on Cal AI's entry id).

Two things get finalized against the FOLLOW-UP capture (see RUNBOOK.md), both
isolated to small, clearly-marked spots:
  1. fetch_diary(): the Firestore collection path + date field for the runQuery.
  2. _extract(): which fields on a diary document hold the food object / logged
     time / image id. The adapter below handles the obvious shapes; confirm the
     names against one real document and adjust.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

from psycopg.types.json import Jsonb

from ingest_calai.client import CalaiAuth, firestore_run_query
from ingest_calai.transforms import food_to_log_row
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.tz import local_date
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

# CONFIRM against the follow-up capture. Firebase apps usually nest the diary
# under the user, e.g. users/<uid>/foods or food_logs filtered by userId. Set
# CALAI_DIARY_COLLECTION once the Firestore read is captured.
DIARY_COLLECTION = os.environ.get("CALAI_DIARY_COLLECTION", "")
# Firestore field the entry is dated by — confirmed `date` on real Cal AI docs.
DIARY_DATE_FIELD = os.environ.get("CALAI_DIARY_DATE_FIELD", "date")


def _parse_ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _extract(entry: dict) -> dict | None:
    """Map one decoded Firestore diary document -> the pieces we need.

    Returns {entry_id, logged_at, food, image_id, health_score} or None to skip.
    The Cal AI food object lives either at the top level of the doc or under a
    'food'/'foodData'/'data' key — finalize against a real document.
    """
    name = entry.get("_name") or ""
    entry_id = entry.get("id") or (name.rsplit("/", 1)[-1] if name else None)
    if not entry_id:
        return None
    # The Cal AI diary doc IS the food object (top-level servingCalories/quantity);
    # the /v6 analysis payload nests it under foodData/food/data. Accept both.
    def _is_food(d):
        return isinstance(d, dict) and (d.get("calories") is not None
                                        or d.get("servingCalories") is not None)
    food = entry.get("foodData") or entry.get("food") or entry.get("data")
    if not _is_food(food):
        food = entry if _is_food(entry) else None
    if food is None:
        return None
    logged_at = _parse_ts(entry.get("date") or entry.get(DIARY_DATE_FIELD)
                          or entry.get("createdAt") or entry.get("loggedAt")
                          or entry.get("timestamp"))
    return {
        "entry_id": str(entry_id),
        "logged_at": logged_at,
        "food": food,
        "image_id": entry.get("image") or entry.get("imageId") or entry.get("photoId"),
        "health_score": (entry.get("healthRating") or entry.get("healthScore")
                         or entry.get("health_score")),
    }


def ingest_diary_entries(entries: list[dict], *, user_id: str | None = None) -> int:
    """Upsert a batch of decoded Firestore diary documents into raw_calai_food +
    fact_food_log, then recompute fact_food_daily for the touched days. Returns
    the number of fact_food_log rows written."""
    raw_rows: list[dict] = []
    fact_rows: list[dict] = []
    days: set[date] = set()
    now = datetime.now(UTC)
    for entry in entries:
        ex = _extract(entry)
        if ex is None:
            continue
        row = food_to_log_row(ex["food"], entry_id=ex["entry_id"], logged_at=ex["logged_at"],
                              image_id=ex["image_id"], health_score=ex["health_score"])
        # fact_food_log.eaten_at and food_name are NOT NULL — skip (don't crash the
        # whole batch on) an entry missing either.
        if row.get("eaten_at") is None or not row.get("food_name"):
            log.warning("calai.skip_incomplete_entry", entry_id=ex["entry_id"],
                        has_time=ex["logged_at"] is not None, has_name=bool(row.get("food_name")))
            continue
        # Bucket the day in LOCAL time (America/New_York), not the process tz, so
        # late-night entries land on the right calendar day and match mart_daily.
        d = local_date(ex["logged_at"])
        days.add(d)
        raw_rows.append({
            "entry_id": ex["entry_id"], "user_id": user_id, "logged_at": ex["logged_at"],
            "day": d, "payload": Jsonb(entry), "fetched_at": now,
        })
        row["micros"] = Jsonb(row["micros"])
        fact_rows.append(row)

    if not fact_rows:
        return 0
    with tx() as c:
        upsert_rows("raw_calai_food", raw_rows, conflict_cols=["entry_id"],
                    update_cols=["user_id", "logged_at", "day", "payload", "fetched_at"],
                    connection=c)
        upsert_rows("fact_food_log", fact_rows, conflict_cols=["source_row_hash"],
                    connection=c)
        _rollup_daily(c, sorted(days))
    return len(fact_rows)


def _rollup_daily(c, days: list[date]) -> None:
    """Recompute fact_food_daily (source='calai') from Cal AI fact_food_log rows
    for the given days. fact_food_daily is keyed by day; Cal AI and the frozen
    Cronometer history don't overlap, so this is a clean per-day upsert."""
    if not days:
        return
    with c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_food_daily
              (day, energy_kcal, protein_g, carbs_g, net_carbs_g, fiber_g, fat_g,
               saturated_fat_g, sodium_mg, alcohol_g, caffeine_mg, source, updated_at)
            SELECT day,
                   SUM(energy_kcal), SUM(protein_g), SUM(carbs_g), SUM(net_carbs_g),
                   SUM(fiber_g), SUM(fat_g), SUM(saturated_fat_g), SUM(sodium_mg),
                   SUM(alcohol_g), SUM(caffeine_mg), 'calai', now()
            FROM fact_food_log
            WHERE source = 'calai' AND day = ANY(%s)
            GROUP BY day
            ON CONFLICT (day) DO UPDATE SET
              energy_kcal = EXCLUDED.energy_kcal, protein_g = EXCLUDED.protein_g,
              carbs_g = EXCLUDED.carbs_g, net_carbs_g = EXCLUDED.net_carbs_g,
              fiber_g = EXCLUDED.fiber_g, fat_g = EXCLUDED.fat_g,
              saturated_fat_g = EXCLUDED.saturated_fat_g, sodium_mg = EXCLUDED.sodium_mg,
              alcohol_g = EXCLUDED.alcohol_g, caffeine_mg = EXCLUDED.caffeine_mg,
              source = 'calai', updated_at = now()
            """,
            [days],
        )


def fetch_diary(auth: CalaiAuth, since: date, until: date) -> list[dict]:
    """Run the Firestore query for the user's diary between two dates.
    FINALIZE the collection path + date field against the follow-up capture."""
    if not DIARY_COLLECTION:
        raise RuntimeError(
            "CALAI_DIARY_COLLECTION is not set. Capture a Firestore diary read "
            "(see RUNBOOK.md) to learn the collection path, then set it in .env."
        )
    structured_query = {
        "from": [{"collectionId": DIARY_COLLECTION}],
        "where": {"compositeFilter": {"op": "AND", "filters": [
            {"fieldFilter": {"field": {"fieldPath": DIARY_DATE_FIELD}, "op": "GREATER_THAN_OR_EQUAL",
                             "value": {"timestampValue": f"{since.isoformat()}T00:00:00Z"}}},
            {"fieldFilter": {"field": {"fieldPath": DIARY_DATE_FIELD}, "op": "LESS_THAN",
                             "value": {"timestampValue": f"{until.isoformat()}T00:00:00Z"}}},
        ]}},
        "orderBy": [{"field": {"fieldPath": DIARY_DATE_FIELD}, "direction": "DESCENDING"}],
    }
    return firestore_run_query(auth, structured_query)


def run_all(backfill_days: int = 7) -> dict:
    """Auth -> fetch the diary window -> ingest. Mirrors the other ingesters."""
    auth = CalaiAuth()
    until = datetime.now(UTC).date() + timedelta(days=1)
    since = until - timedelta(days=backfill_days + 1)
    with ingestion_run("calai", "diary", since=since.isoformat(), until=until.isoformat()) as run:
        entries = fetch_diary(auth, since, until)
        run.fetched(len(entries))
        n = ingest_diary_entries(entries, user_id=auth.user_id)
        run.upserted(n)
    return {"fetched": len(entries), "written": n}


def login(refresh_token: str, user_id: str | None = None) -> None:
    """Bootstrap: store a Firebase refresh token (from a login capture) so the
    ingester can mint ID tokens. The Web API key goes in CALAI_FIREBASE_API_KEY."""
    from ingest_calai.client import _save_tokens
    _save_tokens(id_token="", refresh_token=refresh_token,
                 expires_at=datetime.now(UTC), user_id=user_id)
    log.info("calai.login_stored", user_id=user_id)
