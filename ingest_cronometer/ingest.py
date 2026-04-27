"""Cronometer ingestion: subprocess → CSV → parse → raw_* → fact_*.

One ingestion_runs row per (data_type, window). Failures in one data type
don't abort the others. Auth failures from the Go binary are logged with
full stderr but treated as non-fatal — the scheduler keeps running.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from psycopg.types.json import Jsonb

from ingest_cronometer import parsers
from ingest_cronometer.exporter import ExporterError, export
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run, last_successful_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)


DEFAULT_INCREMENTAL_DAYS = 2  # nightly: re-pull yesterday + day before
DEFAULT_BACKFILL_DAYS = 365


def _window(backfill_days: int | None, data_type: str) -> tuple[date, date]:
    end = date.today()
    if backfill_days is not None:
        return end - timedelta(days=backfill_days), end
    last = last_successful_run("cronometer", data_type)
    if last and last.get("started_at"):
        # Use the configured incremental window — Cronometer edits propagate
        # quickly, so DEFAULT_INCREMENTAL_DAYS=2 is enough for a nightly job.
        return end - timedelta(days=DEFAULT_INCREMENTAL_DAYS), end
    return end - timedelta(days=DEFAULT_BACKFILL_DAYS), end


# ---- per-data-type pipelines -----------------------------------------------
def ingest_servings(*, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "servings")
    with ingestion_run(
        "cronometer", "servings", start=str(start), end=str(end)
    ) as run:
        try:
            csv_text = export("servings", start, end)
        except ExporterError as e:
            run.add_metadata(stderr=e.stderr[:1000])
            raise

        # Raw payload per day so re-running is idempotent.
        per_day = _split_csv_by_day(csv_text, day_col="Day")
        with tx() as c:
            for day, payload_csv in per_day.items():
                _upsert_raw_day(c, "raw_cronometer_servings", day, payload_csv)

        rows = parsers.parse_servings(csv_text)
        run.fetched(len(rows))
        if not rows:
            return 0

        with tx() as c:
            upsert_rows(
                "fact_food_log",
                [_with_updated(r) for r in rows],
                conflict_cols=["source_row_hash"],
                update_cols=[
                    "eaten_at", "meal_group", "food_name", "amount", "unit",
                    "energy_kcal", "protein_g", "carbs_g", "net_carbs_g",
                    "fiber_g", "sugar_g", "fat_g", "saturated_fat_g",
                    "sodium_mg", "potassium_mg", "caffeine_mg", "alcohol_g",
                    "micros", "updated_at",
                ],
                connection=c,
            )

        run.upserted(len(rows))
        return len(rows)


def ingest_daily_nutrition(*, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "daily-nutrition")
    with ingestion_run(
        "cronometer", "daily-nutrition", start=str(start), end=str(end)
    ) as run:
        try:
            csv_text = export("daily-nutrition", start, end)
        except ExporterError as e:
            run.add_metadata(stderr=e.stderr[:1000])
            raise

        per_day = _split_csv_by_day(csv_text, day_col="Date")
        with tx() as c:
            for day, payload_csv in per_day.items():
                _upsert_raw_day(c, "raw_cronometer_daily_nutrition", day, payload_csv)

        rows = parsers.parse_daily_nutrition(csv_text)
        run.fetched(len(rows))
        if not rows:
            return 0

        with tx() as c:
            upsert_rows(
                "fact_food_daily",
                [_with_updated(r) for r in rows],
                conflict_cols=["day"],
                connection=c,
            )

        run.upserted(len(rows))
        return len(rows)


def ingest_biometrics(*, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "biometrics")
    with ingestion_run(
        "cronometer", "biometrics", start=str(start), end=str(end)
    ) as run:
        try:
            csv_text = export("biometrics", start, end)
        except ExporterError as e:
            run.add_metadata(stderr=e.stderr[:1000])
            raise

        per_day = _split_csv_by_day(csv_text, day_col="Date")
        with tx() as c:
            for day, payload_csv in per_day.items():
                _upsert_raw_day(c, "raw_cronometer_biometrics", day, payload_csv)

        rows = parsers.parse_biometrics(csv_text)
        run.fetched(len(rows))
        if not rows:
            return 0

        with tx() as c:
            upsert_rows(
                "fact_biometric",
                [_with_updated(r) for r in rows],
                conflict_cols=["source_row_hash"],
                connection=c,
            )

        run.upserted(len(rows))
        return len(rows)


# ---- exercises (raw only — no current consumers) --------------------------
def ingest_exercises(*, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "exercises")
    with ingestion_run(
        "cronometer", "exercises", start=str(start), end=str(end)
    ) as run:
        try:
            csv_text = export("exercises", start, end)
        except ExporterError as e:
            run.add_metadata(stderr=e.stderr[:1000])
            raise

        per_day = _split_csv_by_day(csv_text, day_col="Day")
        with tx() as c:
            for day, payload_csv in per_day.items():
                _upsert_raw_day(c, "raw_cronometer_exercises", day, payload_csv)

        run.fetched(len(per_day))
        run.upserted(len(per_day))
        return len(per_day)


# ---- orchestration ---------------------------------------------------------
def run_all(*, backfill_days: int | None = None) -> dict:
    out: dict[str, int | str] = {}
    pipelines: list[tuple[str, Callable[..., int]]] = [
        ("servings", ingest_servings),
        ("daily-nutrition", ingest_daily_nutrition),
        ("biometrics", ingest_biometrics),
        ("exercises", ingest_exercises),
    ]
    for name, fn in pipelines:
        try:
            out[name] = fn(backfill_days=backfill_days)
        except Exception as e:  # noqa: BLE001
            log.exception("cronometer.pipeline.failed", pipeline=name)
            out[name] = f"FAILED: {type(e).__name__}: {e}"
    return out


# ---- helpers ---------------------------------------------------------------
def _with_updated(r: dict) -> dict:
    r = dict(r)
    r["updated_at"] = datetime.now(timezone.utc)
    # Wrap any raw dict fields in Jsonb so psycopg knows to serialize them
    # as JSONB. Currently only `micros` qualifies.
    if isinstance(r.get("micros"), dict):
        r["micros"] = Jsonb(r["micros"])
    return r


def _split_csv_by_day(csv_text: str, *, day_col: str) -> dict[date, str]:
    """Bucket the CSV's lines by date so each day gets its own raw row.
    Idempotent: re-runs upsert per (day, table)."""
    import csv as _csv

    out: dict[date, list[list[str]]] = {}
    reader = _csv.reader(csv_text.splitlines())
    rows = list(reader)
    if not rows:
        return {}
    header = rows[0]
    try:
        idx = header.index(day_col)
    except ValueError:
        return {}
    for row in rows[1:]:
        if not row or idx >= len(row):
            continue
        try:
            d = date.fromisoformat(row[idx].strip())
        except ValueError:
            continue
        out.setdefault(d, [header])
        out[d].append(row)

    # Re-emit each bucket as CSV text (so payload is round-trippable).
    import io as _io

    out_text: dict[date, str] = {}
    for d, lines in out.items():
        sio = _io.StringIO()
        _csv.writer(sio).writerows(lines)
        out_text[d] = sio.getvalue()
    return out_text


def _upsert_raw_day(c, table: str, day: date, csv_payload: str) -> None:
    payload = {"csv": csv_payload}
    with c.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {table} (day, payload, fetched_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (day) DO UPDATE SET
              payload = EXCLUDED.payload,
              fetched_at = now()
            """,
            [day, json.dumps(payload)],
        )
