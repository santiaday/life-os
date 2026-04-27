"""Whoop ingestion: API → raw_* → fact_*.

For each data type:
  1. Iterate API records over a time window.
  2. Upsert raw_* (payload as JSONB, natural-key UNIQUE).
  3. Resolve raw_id surrogate via SELECT ... WHERE natural_key = ANY(...).
  4. Upsert fact_* with raw_id wired in.

Keeping raw and fact upserts in two passes (rather than one combined
RETURNING + INSERT) lets us re-run fact derivation against existing raw
without re-fetching.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from psycopg.types.json import Jsonb

from ingest_whoop import transforms
from ingest_whoop.client import WhoopClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run, last_successful_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

# ---- Defaults ---------------------------------------------------------------
DEFAULT_INCREMENTAL_LOOKBACK_DAYS = 3  # always include some overlap for late edits
DEFAULT_BACKFILL_DAYS = 365


def _window(backfill_days: int | None, source: str, data_type: str) -> tuple[datetime, datetime]:
    """Compute (start, end) for a fetch.

    - If backfill_days given: now - backfill_days .. now.
    - Else: max(last successful run.started_at, now - lookback) .. now.
    """
    end = datetime.now(timezone.utc)
    if backfill_days is not None:
        return end - timedelta(days=backfill_days), end

    last = last_successful_run(source, data_type)
    if last and last.get("started_at"):
        # Re-fetch from N days before last success — Whoop sometimes back-edits
        # older recoveries when the band syncs.
        start = last["started_at"] - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK_DAYS)
    else:
        start = end - timedelta(days=DEFAULT_BACKFILL_DAYS)
    return start, end


# ---- Per-source pipelines ---------------------------------------------------
def ingest_cycles(client: WhoopClient, *, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "whoop", "cycle")
    with ingestion_run("whoop", "cycle", start=start.isoformat(), end=end.isoformat()) as run:
        records = list(client.cycles(start, end))
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [
            {"cycle_id": int(r["id"]), "payload": Jsonb(r)} for r in records
        ]
        with tx() as c:
            upsert_rows(
                "raw_whoop_cycle",
                raw_rows,
                conflict_cols=["cycle_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            id_map = _id_map(c, "raw_whoop_cycle", "cycle_id", [r["cycle_id"] for r in raw_rows])
            fact_rows = []
            for r in records:
                row = transforms.transform_cycle(r)
                row["raw_id"] = id_map.get(row["cycle_id"])
                row["updated_at"] = datetime.now(timezone.utc)
                fact_rows.append(row)
            upsert_rows("fact_cycle", fact_rows, conflict_cols=["cycle_id"], connection=c)

        run.upserted(len(fact_rows))
        return len(fact_rows)


def ingest_recoveries(client: WhoopClient, *, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "whoop", "recovery")
    with ingestion_run("whoop", "recovery", start=start.isoformat(), end=end.isoformat()) as run:
        records = list(client.recovery(start, end))
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [
            {"cycle_id": int(r["cycle_id"]), "payload": Jsonb(r)} for r in records
        ]
        with tx() as c:
            upsert_rows(
                "raw_whoop_recovery",
                raw_rows,
                conflict_cols=["cycle_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            id_map = _id_map(
                c, "raw_whoop_recovery", "cycle_id", [r["cycle_id"] for r in raw_rows]
            )
            fact_rows = []
            for r in records:
                row = transforms.transform_recovery(r)
                if row["day"] is None:
                    log.warning("whoop.recovery.no_day", cycle_id=row["cycle_id"])
                    continue
                row["raw_id"] = id_map.get(row["cycle_id"])
                row["updated_at"] = datetime.now(timezone.utc)
                fact_rows.append(row)

            _sanity_check_hrv(fact_rows)
            upsert_rows("fact_recovery", fact_rows, conflict_cols=["cycle_id"], connection=c)

        run.upserted(len(fact_rows))
        return len(fact_rows)


def ingest_sleep(client: WhoopClient, *, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "whoop", "sleep")
    with ingestion_run("whoop", "sleep", start=start.isoformat(), end=end.isoformat()) as run:
        records = list(client.sleep(start, end))
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [{"sleep_id": r["id"], "payload": Jsonb(r)} for r in records]
        with tx() as c:
            upsert_rows(
                "raw_whoop_sleep",
                raw_rows,
                conflict_cols=["sleep_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            id_map = _id_map(c, "raw_whoop_sleep", "sleep_id", [r["sleep_id"] for r in raw_rows])
            fact_rows = []
            for r in records:
                row = transforms.transform_sleep(r)
                row["raw_id"] = id_map.get(row["sleep_id"])
                row["updated_at"] = datetime.now(timezone.utc)
                fact_rows.append(row)
            upsert_rows("fact_sleep", fact_rows, conflict_cols=["sleep_id"], connection=c)

        run.upserted(len(fact_rows))
        return len(fact_rows)


def ingest_workouts(client: WhoopClient, *, backfill_days: int | None = None) -> int:
    start, end = _window(backfill_days, "whoop", "workout")
    with ingestion_run("whoop", "workout", start=start.isoformat(), end=end.isoformat()) as run:
        records = list(client.workouts(start, end))
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [{"workout_id": r["id"], "payload": Jsonb(r)} for r in records]
        with tx() as c:
            upsert_rows(
                "raw_whoop_workout",
                raw_rows,
                conflict_cols=["workout_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            id_map = _id_map(
                c, "raw_whoop_workout", "workout_id", [r["workout_id"] for r in raw_rows]
            )
            fact_rows = []
            for r in records:
                row = transforms.transform_workout(r)
                row["raw_id"] = id_map.get(row["workout_id"])
                row["updated_at"] = datetime.now(timezone.utc)
                fact_rows.append(row)
            upsert_rows("fact_workout", fact_rows, conflict_cols=["workout_id"], connection=c)

        run.upserted(len(fact_rows))
        return len(fact_rows)


def ingest_profile(client: WhoopClient) -> int:
    """One-shot: profile + body measurement go into raw_whoop_profile.
    No fact table — just keep the JSONB for reference."""
    with ingestion_run("whoop", "profile") as run:
        prof = client.profile()
        body = client.body_measurement()
        payload = {"profile": prof, "body": body}
        with tx() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO raw_whoop_profile (payload) VALUES (%s::jsonb)",
                [json.dumps(payload)],
            )
        run.fetched(1)
        run.upserted(1)
        return 1


# ---- Orchestration ----------------------------------------------------------
def run_all(*, backfill_days: int | None = None) -> dict:
    """Run every Whoop pipeline, returning per-type counts. Failures in one
    pipeline don't abort the others — each one is its own ingestion_run."""
    results: dict[str, int | str] = {}
    pipelines: list[tuple[str, Callable[[WhoopClient], int]]] = [
        ("cycle", lambda c: ingest_cycles(c, backfill_days=backfill_days)),
        ("recovery", lambda c: ingest_recoveries(c, backfill_days=backfill_days)),
        ("sleep", lambda c: ingest_sleep(c, backfill_days=backfill_days)),
        ("workout", lambda c: ingest_workouts(c, backfill_days=backfill_days)),
        ("profile", lambda c: ingest_profile(c)),
    ]
    with WhoopClient() as client:
        for name, fn in pipelines:
            try:
                results[name] = fn(client)
            except Exception as e:
                log.exception("whoop.pipeline.failed", pipeline=name)
                results[name] = f"FAILED: {type(e).__name__}: {e}"
    return results


# ---- Helpers ----------------------------------------------------------------
def _id_map(
    connection, table: str, key_col: str, keys: Iterable
) -> dict:
    """In-transaction lookup of {key_col: id} for a set of keys."""
    keys = list(keys)
    if not keys:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT {key_col}, id FROM {table} WHERE {key_col} = ANY(%s)",
            [keys],
        )
        return {row[key_col]: row["id"] for row in cur.fetchall()}


def _sanity_check_hrv(rows: list[dict]) -> None:
    """Catch the seconds-vs-ms unit slip the spec warns about. Healthy human
    RMSSD is roughly 10-200 ms. If a non-trivial fraction falls outside, log a
    loud warning so we notice on first ingest."""
    values = [r["hrv_rmssd_ms"] for r in rows if r.get("hrv_rmssd_ms") is not None]
    if not values:
        return
    implausible = [v for v in values if v < 5 or v > 300]
    if len(implausible) > len(values) * 0.5:
        log.error(
            "whoop.recovery.hrv_implausible",
            sample_values=values[:5],
            implausible_count=len(implausible),
            total=len(values),
            hint="Whoop API may have changed units; review transforms.hrv_to_ms.",
        )
