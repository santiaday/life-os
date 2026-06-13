"""Whoop journal ingestion: API → raw → fact/dim.

Three pipelines:

  ingest_behavior_catalog()
      Weekly. Pulls /v3/journals/behaviors, upserts dim_whoop_behavior.

  ingest_journal_day(day)
      Pulls /v3/journals/drafts/mobile/{day} and upserts:
        - raw_whoop_journal           (one row per day, full JSON)
        - fact_journal_day            (typed day-level pivot: notes, cycle_id…)
        - fact_habit_log              (one row per tracked behavior + autofill)
        - fact_food_daily_apple_health (Apple Health macros via Whoop)
        - dim_whoop_behavior          (synth rows for autofill behaviors not
                                       in the catalog yet — overwritten on
                                       the next catalog refresh)

  ingest_journal_window(...)
      Loop ingest_journal_day across a date range. Default is a 2-day rolling
      rebackfill; --backfill N pulls today + N days back; --start/--end is
      an explicit inclusive range for one-time historical pulls.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from psycopg.types.json import Jsonb

from ingest_whoop_journal import transforms
from ingest_whoop_journal.auth import WhoopAuth
from ingest_whoop_journal.client import WhoopJournalClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

# Daily rebackfill window. Whoop's journal API doesn't expose a day's
# tracked_behaviors[] until the user's *next* sleep closes the cycle, so a
# cron firing at 5:35 AM ET against "yesterday" routinely sees `no_entry`.
# A 7-day rolling re-pull always catches the prior day on the next run, plus
# any late edits within the week. ~7 GETs/day is trivial.
DEFAULT_INCREMENTAL_DAYS = 6  # 6 days back + today = 7 days total


# ---- behavior catalog ------------------------------------------------------
def ingest_behavior_catalog(*, auth: WhoopAuth | None = None) -> int:
    with ingestion_run("whoop_journal", "behavior_catalog") as run:
        with WhoopJournalClient(auth=auth) as client:
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
            row["updated_at"] = datetime.now(UTC)
            rows.append(row)

        if not rows:
            return 0

        with tx() as c:
            upsert_rows(
                "dim_whoop_behavior",
                rows,
                conflict_cols=["behavior_id"],
                update_cols=[
                    "internal_name", "title", "question_text", "category",
                    "behavior_type", "question_type", "magnitude_type",
                    "magnitude_unit", "magnitude_min", "magnitude_max",
                    "status", "payload", "updated_at",
                ],
                connection=c,
            )

        run.upserted(len(rows))
        return len(rows)


def _ensure_dim_rows_for_autofill(
    autofill_rows: list[dict],
    integrations_inputs: list[dict],
) -> None:
    """Insert minimal dim_whoop_behavior rows for autofill behavior_ids that
    might not be in the catalog yet. Uses ON CONFLICT DO NOTHING so we don't
    overwrite real catalog entries with our synthesized placeholders."""
    if not autofill_rows:
        return
    seen_bids = set()
    synth: list[dict] = []
    for ar in autofill_rows:
        bid = ar.get("behavior_id")
        if bid is None or bid in seen_bids:
            continue
        seen_bids.add(bid)
        # Find the source name by matching against the original integrations input.
        name = None
        for inp in integrations_inputs:
            cand_bid = (
                inp.get("behavior_tracker_id")
                or (inp.get("behavior_tracker") or {}).get("id")
                or inp.get("behavior_id")
                or (inp.get("behavior") or {}).get("id")
            )
            if cand_bid == bid:
                name = inp.get("name") or inp.get("source_tracking_key") or inp.get("input_name")
                break
        if not name:
            name = ar.get("habit_key") or f"autofill-{bid}"
        row = transforms.synthesize_dim_from_autofill(name, bid)
        row["payload"] = Jsonb({"synthesized_from": "autofill", "name": name})
        row["updated_at"] = datetime.now(UTC)
        synth.append(row)

    if not synth:
        return

    with tx() as c, c.cursor() as cur:
        for row in synth:
            cols = list(row.keys())
            cur.execute(
                f"""
                INSERT INTO dim_whoop_behavior ({", ".join(cols)})
                VALUES ({", ".join(["%s"] * len(cols))})
                ON CONFLICT (behavior_id) DO NOTHING
                """,
                [row[c_] for c_ in cols],
            )


# ---- per-day ingestion -----------------------------------------------------
def ingest_journal_day(day: date, *, client: WhoopJournalClient | None = None) -> dict:
    """Returns a small dict summarizing what landed for the day."""
    counts = {
        "raw": 0, "journal_day": 0, "habit_log": 0,
        "habit_log_autofill": 0, "food_daily_ah": 0, "no_entry": False,
    }

    if client is None:
        with WhoopJournalClient() as c:
            payload = c.journal_draft(day)
    else:
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

    # ---- fact_journal_day (typed day-level pivot) --------------------------
    day_row = transforms.transform_journal_day(payload, day)
    if day_row is not None:
        if day_row.get("sleep_during") is not None:
            day_row["sleep_during"] = Jsonb(day_row["sleep_during"])
        with tx() as c:
            upsert_rows(
                "fact_journal_day",
                [day_row],
                conflict_cols=["day"],
                update_cols=[
                    "journal_entry_id", "cycle_id", "notes",
                    "user_reviewed", "sleep_during",
                ],
                connection=c,
            )
        counts["journal_day"] = 1

    # ---- fact_habit_log (tracked behaviors + autofill) ---------------------
    journal = payload.get("journal") or {}
    tracked = journal.get("tracked_behaviors") or []
    integrations_inputs = (payload.get("integrations") or {}).get("tracker_inputs") or []

    fact_rows: list[dict] = []
    for t in tracked:
        row = transforms.transform_tracked_behavior(t, day)
        if row is None:
            continue
        row["updated_at"] = datetime.now(UTC)
        fact_rows.append(row)

    autofill_rows: list[dict] = []
    for inp in integrations_inputs:
        row = transforms.transform_autofill_input(inp, day)
        if row is None:
            continue
        row["updated_at"] = datetime.now(UTC)
        autofill_rows.append(row)

    # Synth dim rows for any autofill behavior_ids that aren't in the
    # catalog yet, so the FK doesn't trip on the fact upsert.
    _ensure_dim_rows_for_autofill(autofill_rows, integrations_inputs)

    if fact_rows or autofill_rows:
        # Tracked-behavior rows take precedence over autofill rows for the
        # same (day, behavior_id) — user-entered data is more trustworthy
        # than Apple Health imports. Apply tracked first into the dedupe
        # map so autofill can only fill empty slots.
        by_key: dict[tuple, dict] = {}
        for r in autofill_rows:
            by_key[(r["day"], r["behavior_id"])] = r
        for r in fact_rows:
            by_key[(r["day"], r["behavior_id"])] = r
        all_rows = list(by_key.values())

        with tx() as c:
            upsert_rows(
                "fact_habit_log",
                all_rows,
                conflict_cols=["day", "behavior_id"],
                update_cols=[
                    "habit_key", "source", "whoop_journal_entry_id", "whoop_cycle_id",
                    "answered_yes", "magnitude_value", "magnitude_unit",
                    "time_input_value", "user_reviewed", "notes",
                    "source_row_hash", "updated_at",
                ],
                connection=c,
            )
        counts["habit_log"] = len(fact_rows)
        counts["habit_log_autofill"] = sum(
            1 for r in all_rows if r["source"] == "whoop_apple_health"
        )

    # ---- fact_food_daily_apple_health -------------------------------------
    ah_row = transforms.transform_tracker_inputs(payload, day)
    if ah_row is not None:
        ah_row["payload"] = Jsonb(ah_row.get("payload") or [])
        ah_row["updated_at"] = datetime.now(UTC)
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
def ingest_journal_window(
    *,
    backfill_days: int | None = None,
    start: date | None = None,
    end: date | None = None,
    auth: WhoopAuth | None = None,
) -> dict:
    """Pull a window of days. Three call modes:

      - default (no args): today + DEFAULT_INCREMENTAL_DAYS days back.
      - backfill_days=N:   today + N days back (so N+1 total).
      - start=..., end=...: explicit inclusive range.

    One ingestion_runs row covers the whole window. Per-day failures land in
    the per-day metadata so one bad day doesn't abort the rest.
    """
    if start is not None or end is not None:
        if start is None or end is None:
            raise ValueError("start and end must be provided together")
        if end < start:
            raise ValueError(f"end ({end}) is before start ({start})")
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        end = date.today()
        n = backfill_days if backfill_days is not None else DEFAULT_INCREMENTAL_DAYS
        start = end - timedelta(days=n)
        days = [start + timedelta(days=i) for i in range(n + 1)]

    auth = auth or WhoopAuth()
    auth.ensure_fresh()  # fail fast (WhoopAuthExpired) before opening a run row

    with ingestion_run(
        "whoop_journal", "drafts",
        start=str(start), end=str(end), days=len(days),
    ) as run:
        per_day: dict[str, dict] = {}
        total_habits = 0
        with WhoopJournalClient(auth=auth) as client:
            for d in reversed(days):  # newest first
                try:
                    counts = ingest_journal_day(d, client=client)
                    per_day[d.isoformat()] = counts
                    total_habits += counts.get("habit_log", 0) + counts.get("habit_log_autofill", 0)
                except Exception as e:
                    log.exception("whoop_journal.day_failed", day=str(d))
                    per_day[d.isoformat()] = {"error": f"{type(e).__name__}: {e}"}

        run.fetched(len(days))
        run.upserted(total_habits)
        run.add_metadata(per_day=per_day)
        return per_day


def run_all(
    *,
    backfill_days: int | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """Catalog + window. Reuses a single WhoopAuth instance so the DB read
    happens once per run."""
    auth = WhoopAuth()
    out: dict = {}
    try:
        out["behavior_catalog"] = ingest_behavior_catalog(auth=auth)
    except Exception as e:
        log.exception("whoop_journal.catalog_failed")
        out["behavior_catalog"] = f"FAILED: {type(e).__name__}: {e}"
    try:
        out["drafts"] = ingest_journal_window(
            backfill_days=backfill_days, start=start, end=end, auth=auth,
        )
    except Exception as e:
        log.exception("whoop_journal.drafts_failed")
        out["drafts"] = f"FAILED: {type(e).__name__}: {e}"
    return out
