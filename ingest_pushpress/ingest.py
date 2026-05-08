"""PushPress ingestion: API → raw_pushpress_workout_of_day → fact_pushpress_*.

Daily flow (see SPEC.md PushPress section):

  1. Refresh class types  (dim_pushpress_class_type)
  2. For each class type × each date in [today-7 .. today+7]:
       fetch GetWorkoutOfDay
       hash payload; skip if unchanged from last fetch (no fetched_at churn)
       upsert raw_pushpress_workout_of_day
       if non-empty: upsert fact_pushpress_session + replace fact_pushpress_part rows
  3. Best-effort link to fact_workout (Whoop) by class_date — same physical
     session shows up on the band on the same day.

Idempotent: re-running is a no-op for unchanged programming. The window is
chosen to catch (a) coach edits in the past week and (b) the next week's
forward programming as soon as the gym publishes it.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ingest_pushpress import transforms
from ingest_pushpress.client import PushPressAPIError, PushPressClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

DEFAULT_WINDOW_PAST = 7
DEFAULT_WINDOW_FUTURE = 7


# ---- top-level orchestration ----------------------------------------------
def run_all(
    *,
    days_past: int | None = None,
    days_future: int | None = None,
    skip_class_types: bool = False,
) -> dict:
    """Run the full PushPress sync. Each phase is its own ingestion_runs row
    so a failure in one doesn't mask the others.

    days_past / days_future: window in days around today. Defaults pull
    today-7 through today+7 (matches the spec)."""
    out: dict[str, int | str] = {}
    days_past = days_past if days_past is not None else DEFAULT_WINDOW_PAST
    days_future = days_future if days_future is not None else DEFAULT_WINDOW_FUTURE

    with PushPressClient() as client:
        if not skip_class_types:
            try:
                out["class_types"] = ingest_class_types(client)
            except Exception as e:  # noqa: BLE001
                log.exception("pushpress.class_types.failed")
                out["class_types"] = f"FAILED: {type(e).__name__}: {e}"

        try:
            out["workouts"] = ingest_workouts(
                client, days_past=days_past, days_future=days_future
            )
        except Exception as e:  # noqa: BLE001
            log.exception("pushpress.workouts.failed")
            out["workouts"] = f"FAILED: {type(e).__name__}: {e}"

    return out


# ---- class types ----------------------------------------------------------
def ingest_class_types(client: PushPressClient) -> int:
    """Refresh dim_pushpress_class_type. Tiny (3 rows in production) — full
    rewrite via upsert."""
    with ingestion_run("pushpress", "class_types") as run:
        api_rows = client.class_types()
        rows = [transforms.class_type_row(r) for r in api_rows]
        run.fetched(len(rows))
        if not rows:
            return 0
        upsert_rows(
            "dim_pushpress_class_type",
            rows,
            conflict_cols=["uuid"],
            update_cols=[
                "name", "origin", "is_static", "progressive",
                "last_day_num", "fetched_at",
            ],
        )
        # The upsert above doesn't auto-bump fetched_at because we didn't
        # include it in `rows`. Touch it explicitly so freshness checks land.
        with tx() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE dim_pushpress_class_type SET fetched_at = now() "
                "WHERE uuid = ANY(%s)",
                [[r["uuid"] for r in rows]],
            )
        run.upserted(len(rows))
        return len(rows)


# ---- workouts -------------------------------------------------------------
def ingest_workouts(
    client: PushPressClient,
    *,
    days_past: int = DEFAULT_WINDOW_PAST,
    days_future: int = DEFAULT_WINDOW_FUTURE,
) -> dict[str, Any]:
    """Pull the ±N-day workout window for every known class type."""
    today = date.today()
    start = today - timedelta(days=days_past)
    end = today + timedelta(days=days_future)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    class_types = _load_class_types()
    if not class_types:
        log.warning("pushpress.workouts.no_class_types")
        return {"upserted": 0, "skipped": 0, "errors": 0}

    summary: dict[str, Any] = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "calls": 0,
        "upserted": 0,
        "skipped_unchanged": 0,
        "empty_dates": 0,
        "errors": 0,
        "session_rows": 0,
        "part_rows": 0,
        "whoop_links": 0,
    }

    with ingestion_run(
        "pushpress",
        "workouts",
        start=start.isoformat(),
        end=end.isoformat(),
    ) as run:
        for ct in class_types:
            for d in dates:
                summary["calls"] += 1
                try:
                    payloads = client.workout_of_day(d, ct["uuid"])
                except PushPressAPIError as e:
                    log.warning(
                        "pushpress.workout.fetch_failed",
                        class_type=ct["uuid"], date=d.isoformat(), error=str(e),
                    )
                    summary["errors"] += 1
                    continue

                if not payloads:
                    _upsert_empty_raw(ct["uuid"], d)
                    summary["empty_dates"] += 1
                    continue

                # Most common case: single-element list. Iterate to be safe.
                for payload in payloads:
                    try:
                        result = _persist_payload(payload, ct, d)
                    except Exception as e:  # noqa: BLE001
                        log.exception(
                            "pushpress.workout.persist_failed",
                            class_type=ct["uuid"], date=d.isoformat(),
                            error=str(e),
                        )
                        summary["errors"] += 1
                        continue
                    summary["upserted"] += result["upserted"]
                    summary["skipped_unchanged"] += result["skipped"]
                    summary["session_rows"] += result["session_rows"]
                    summary["part_rows"] += result["part_rows"]
                    summary["whoop_links"] += result["whoop_links"]

        run.fetched(summary["calls"])
        run.upserted(summary["upserted"])
        run.add_metadata(**{k: v for k, v in summary.items()
                            if k not in ("calls",)})
    return summary


# ---- helpers --------------------------------------------------------------
def _load_class_types() -> list[dict]:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT uuid, name FROM dim_pushpress_class_type ORDER BY name"
        )
        return cur.fetchall()


def _upsert_empty_raw(class_type_uuid: str, class_date: date) -> None:
    """Persist an 'asked, got nothing' marker. Avoids re-fetching empties on
    every run when a date is permanently a rest day. payload is NULL but the
    row's presence is the signal.

    Idempotent: if a row already exists with is_empty=TRUE we don't bump
    fetched_at (matches the unchanged-payload behavior). Only flips an
    existing non-empty row back to empty (rare — coach unpublished a
    workout) by writing fetched_at fresh."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_pushpress_workout_of_day
              (class_type_uuid, class_date, workout_uid, is_empty,
               payload, payload_hash, fetched_at)
            VALUES (%s, %s, NULL, TRUE, NULL, NULL, now())
            ON CONFLICT (class_type_uuid, class_date) DO UPDATE SET
              workout_uid = NULL,
              is_empty    = TRUE,
              payload     = NULL,
              payload_hash = NULL,
              updated_at_src = NULL,
              fetched_at = CASE
                WHEN raw_pushpress_workout_of_day.is_empty
                  THEN raw_pushpress_workout_of_day.fetched_at
                ELSE now()
              END
            """,
            [class_type_uuid, class_date],
        )


def _persist_payload(payload: dict, ct: dict, class_date: date) -> dict:
    """Land one workout payload across raw + fact tables.

    Returns counters: {upserted, skipped, session_rows, part_rows, whoop_links}.
    Skipped means the payload hash matches what we already had — we still
    update fetched_at but don't rewrite fact rows.
    """
    h = transforms.payload_hash(payload)
    workout_uid = (
        payload.get("workoutUid")
        or payload.get("uid")
        or transforms.synthesize_workout_uid(ct["uuid"], class_date)
    )
    updated_src = transforms.parse_pushpress_ts(payload.get("updatedDate"))

    with tx() as c:
        # Read prior hash so we know whether to rebuild facts.
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT payload_hash FROM raw_pushpress_workout_of_day
                 WHERE class_type_uuid = %s AND class_date = %s
                """,
                [ct["uuid"], class_date],
            )
            prior = cur.fetchone()
        unchanged = prior is not None and prior.get("payload_hash") == h

        # Cheap-dedup path: nothing changed since last fetch — leave fetched_at
        # alone (spec acceptance test) and don't rewrite the fact rows. The
        # ingestion_runs row still records the call, so freshness alerts work.
        if unchanged:
            return {
                "upserted": 0, "skipped": 1,
                "session_rows": 0, "part_rows": 0, "whoop_links": 0,
            }

        upsert_rows(
            "raw_pushpress_workout_of_day",
            [{
                "class_type_uuid": ct["uuid"],
                "class_date": class_date,
                "workout_uid": workout_uid,
                "is_empty": False,
                "payload": Jsonb(payload),
                "payload_hash": h,
                "fetched_at": datetime.now(timezone.utc),
                "updated_at_src": updated_src,
            }],
            conflict_cols=["class_type_uuid", "class_date"],
            update_cols=[
                "workout_uid", "is_empty", "payload", "payload_hash",
                "fetched_at", "updated_at_src",
            ],
            connection=c,
        )

        # Rebuild fact rows for this workout.
        sess = transforms.session_row(
            payload,
            class_type_uuid=ct["uuid"],
            class_type_name=ct.get("name"),
            class_date=class_date,
        )
        whoop_workout_id = _match_whoop_workout(c, class_date)
        sess["whoop_workout_id"] = whoop_workout_id
        upsert_rows(
            "fact_pushpress_session",
            [sess],
            conflict_cols=["workout_uid"],
            update_cols=[k for k in sess.keys() if k != "workout_uid"]
                       + ["fetched_at"],
            connection=c,
        )
        # fetched_at isn't in the dict above — bump it explicitly.
        with c.cursor() as cur:
            cur.execute(
                "UPDATE fact_pushpress_session SET fetched_at = now() "
                "WHERE workout_uid = %s",
                [sess["workout_uid"]],
            )
            cur.execute(
                "DELETE FROM fact_pushpress_part WHERE workout_uid = %s",
                [sess["workout_uid"]],
            )

        parts = transforms.part_rows(
            payload, class_type_uuid=ct["uuid"], class_date=class_date,
        )
        if parts:
            upsert_rows(
                "fact_pushpress_part",
                parts,
                conflict_cols=["part_uid"],
                connection=c,
            )

    return {
        "upserted": 1,
        "skipped": 0,
        "session_rows": 1,
        "part_rows": len(parts) if 'parts' in locals() else 0,
        "whoop_links": 1 if whoop_workout_id else 0,
    }


def _match_whoop_workout(connection, class_date: date) -> str | None:
    """Best-effort link to a Whoop workout on the same class_date. We don't
    know a start_ts for the programmed session (PushPress just has the
    calendar day) so we match by date alone — return the highest-strain
    Whoop workout that day, which in practice is the gym session.

    Returns NULL if there's no Whoop workout that day or fact_workout doesn't
    exist yet."""
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT workout_id
                  FROM fact_workout
                 WHERE (start_ts AT TIME ZONE 'UTC')::date = %s
                    OR (end_ts   AT TIME ZONE 'UTC')::date = %s
                 ORDER BY strain DESC NULLS LAST
                 LIMIT 1
                """,
                [class_date, class_date],
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return None
    return row["workout_id"] if row else None
