"""Whoop journal ingestion: API → raw → fact/dim.

Three pipelines:

  ingest_behavior_catalog()
      Weekly. Pulls the full /v3/journals/behaviors list, upserts to
      dim_whoop_behavior. Idempotent.

  ingest_journal_day(day)
      Pulls /v3/journals/drafts/mobile/{day}, upserts the raw payload by
      day, then derives:
        - fact_habit_log rows (one per tracked behavior)
        - fact_food_daily_apple_health row (if integrations present)

  ingest_journal_window(backfill_days)
      Loop ingest_journal_day across the last N days. Default behavior
      (backfill_days=None) is a 3-day rolling rebackfill that catches
      late edits — Whoop journal entries can be edited days after the fact.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from ingest_whoop_journal import transforms
from ingest_whoop_journal.client import WhoopJournalClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

DEFAULT_INCREMENTAL_DAYS = 3      # rolling window for late edits
DEFAULT_BACKFILL_DAYS = 365


# ---- behavior catalog ------------------------------------------------------
def ingest_behavior_catalog() -> int:
    with ingestion_run("whoop_journal", "behavior_catalog") as run:
        with WhoopJournalClient() as client:
            records = client.behaviors_catalog()
        run.fetched(len(records))
        if not records:
            return 0

        rows: list[dict] = []
        for r in records:
            row = transforms.transform_behavior(r)
            if row is None:
                continue
            row["payload"] = Jsonb(r)
            row["updated_at"] = datetime.now(timezone.utc)
            rows.append(row)

        if not rows:
            return 0

        with tx() as c:
            upsert_rows(
                "dim_whoop_behavior",
                rows,
                conflict_cols=["behavior_id"],
                connection=c,
            )

        run.upserted(len(rows))
        return len(rows)


# ---- per-day ingestion -----------------------------------------------------
def ingest_journal_day(day: date) -> dict:
    """Returns a small dict summarizing what landed for the day."""
    counts = {"raw": 0, "habit_log": 0, "food_daily_ah": 0, "no_entry": False}

    with WhoopJournalClient() as client:
        payload = client.journal_draft(day)

    if not payload:
        counts["no_entry"] = True
        return counts

    # ---- raw upsert (one row per day) --------------------------------------
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_whoop_journal (day, payload, fetched_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (day) DO UPDATE SET
              payload = EXCLUDED.payload,
              fetched_at = now()
            """,
            [day, json.dumps(payload, default=str)],
        )
    counts["raw"] = 1

    # ---- fact_habit_log ----------------------------------------------------
    journal = payload.get("journal") or {}
    tracked = journal.get("tracked_behaviors") or []
    fact_rows: list[dict] = []
    for t in tracked:
        row = transforms.transform_tracked_behavior(t, day)
        if row is None:
            continue
        row["updated_at"] = datetime.now(timezone.utc)
        fact_rows.append(row)

    if fact_rows:
        # Two-pass dedupe: API can technically emit two entries for the same
        # behavior_id within a single day; keep the most recent (last in
        # array). Without this the (day, whoop_behavior_id) UNIQUE trips.
        by_key: dict[tuple, dict] = {}
        for r in fact_rows:
            by_key[(r["day"], r["whoop_behavior_id"])] = r
        fact_rows = list(by_key.values())

        with tx() as c:
            upsert_rows(
                "fact_habit_log",
                fact_rows,
                conflict_cols=["day", "whoop_behavior_id"],
                update_cols=[
                    "habit_key", "whoop_journal_entry_id", "whoop_cycle_id",
                    "answered_yes", "magnitude_value", "magnitude_unit",
                    "time_input_value", "user_reviewed", "notes",
                    "source_row_hash", "updated_at",
                ],
                connection=c,
            )
        counts["habit_log"] = len(fact_rows)

    # ---- fact_food_daily_apple_health -------------------------------------
    ah_row = transforms.transform_tracker_inputs(payload, day)
    if ah_row is not None:
        ah_row["payload"] = Jsonb(ah_row.get("payload") or [])
        ah_row["updated_at"] = datetime.now(timezone.utc)
        with tx() as c:
            upsert_rows(
                "fact_food_daily_apple_health",
                [ah_row],
                conflict_cols=["day"],
                connection=c,
            )
        counts["food_daily_ah"] = 1

    return counts


# ---- windowed ingestion ----------------------------------------------------
def ingest_journal_window(*, backfill_days: int | None = None) -> dict:
    """Pull a window of days. Default (None) = last DEFAULT_INCREMENTAL_DAYS.

    One ingestion_runs row covers the whole window. Per-day failures are
    swallowed into a per-day error map so one bad day doesn't abort the rest.
    """
    end = date.today()
    n = backfill_days if backfill_days is not None else DEFAULT_INCREMENTAL_DAYS
    start = end - timedelta(days=n)

    with ingestion_run(
        "whoop_journal", "drafts",
        start=str(start), end=str(end), days=n + 1,
    ) as run:
        per_day: dict[str, dict] = {}
        total_habits = 0
        for offset in range(n + 1):
            d = end - timedelta(days=offset)
            try:
                counts = ingest_journal_day(d)
                per_day[d.isoformat()] = counts
                total_habits += counts.get("habit_log", 0)
            except Exception as e:  # noqa: BLE001
                log.exception("whoop_journal.day_failed", day=str(d))
                per_day[d.isoformat()] = {"error": f"{type(e).__name__}: {e}"}

        run.fetched(n + 1)
        run.upserted(total_habits)
        run.add_metadata(per_day=per_day)
        return per_day


def run_all(*, backfill_days: int | None = None) -> dict:
    """Catalog + window. Mirrors the shape of other ingester run_all()s."""
    out: dict = {}
    try:
        out["behavior_catalog"] = ingest_behavior_catalog()
    except Exception as e:  # noqa: BLE001
        log.exception("whoop_journal.catalog_failed")
        out["behavior_catalog"] = f"FAILED: {type(e).__name__}: {e}"
    try:
        out["drafts"] = ingest_journal_window(backfill_days=backfill_days)
    except Exception as e:  # noqa: BLE001
        log.exception("whoop_journal.drafts_failed")
        out["drafts"] = f"FAILED: {type(e).__name__}: {e}"
    return out
