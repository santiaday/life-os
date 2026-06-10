"""Whoop private-API ingestion: API -> raw_* -> fact_*.

Three data families, each its own ingestion_run so one failing doesn't abort the
others (mirrors ingest_whoop.run_all):

  trend            -> raw_whoop_trend          -> fact_whoop_metric_daily
  sleep_need       -> raw_whoop_sleep_need     -> fact_whoop_sleep_need
  behavior_impact  -> raw_whoop_behavior_impact-> fact_whoop_behavior_impact

Auth is the shared whoop_private bearer (ingest_whoop_journal.auth.WhoopAuth);
we read it, never refresh it. Same three-pass upsert as ingest_whoop: raw JSONB
on a natural key, resolve the surrogate id, then upsert the typed fact with
raw_id wired in.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ingest_whoop_journal.auth import WhoopAuth, WhoopAuthExpired
from ingest_whoop_private import transforms
from ingest_whoop_private.client import METRICS, WhoopPrivateClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.settings import settings
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

# A six-month trend window covers ~182 days, so each end_date anchor pulls that
# far back. Backfill walks end_date back in 182-day strides; overlapping points
# dedup on (day, metric).
DEFAULT_BACKFILL_DAYS = 365
STRIDE_DAYS = 182


def _local_today() -> date:
    return datetime.now(ZoneInfo(settings.LOCAL_TZ)).date()


def _now() -> datetime:
    return datetime.now(UTC)


def _end_dates(backfill_days: int | None) -> list[date]:
    """end_date anchors to fetch, freshest first. Incremental: [today] (its
    six-month window already reaches ~182 days back). Backfill N days: walk back
    in 182-day strides until the earliest anchor's window covers N days."""
    today = _local_today()
    if not backfill_days:
        return [today]
    anchors = [today]
    earliest_covered = STRIDE_DAYS
    while earliest_covered < backfill_days:
        anchors.append(today - timedelta(days=earliest_covered))
        earliest_covered += STRIDE_DAYS
    return anchors


def _raw_id(connection, table: str, where: dict) -> int | None:
    """Resolve a single surrogate id by an arbitrary natural key."""
    clause = " AND ".join(f"{k} = %s" for k in where)
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT id FROM {table} WHERE {clause} ORDER BY id DESC LIMIT 1",
            list(where.values()),
        )
        row = cur.fetchone()
        return row["id"] if row else None


# ---- pipelines -------------------------------------------------------------
def ingest_trends(
    client: WhoopPrivateClient,
    *,
    metrics: tuple[str, ...] = METRICS,
    backfill_days: int | None = None,
) -> int:
    """Pull per-day trend points for each metric across the end_date anchors and
    upsert the long-format fact_whoop_metric_daily. Per-metric fetch failures are
    captured in run metadata and don't abort the others (auth expiry does)."""
    anchors = _end_dates(backfill_days)
    with ingestion_run(
        "whoop_private",
        "trend",
        anchors=[a.isoformat() for a in anchors],
        metric_count=len(metrics),
    ) as run:
        fact_by_day: dict[tuple[date, str], dict] = {}
        fetched = 0
        errors: dict[str, str] = {}
        with tx() as c:
            for anchor in anchors:  # freshest first -> wins on overlap
                for metric in metrics:
                    try:
                        payload = client.trend(metric, anchor)
                    except WhoopAuthExpired:
                        raise
                    except Exception as e:
                        errors[f"{metric}@{anchor.isoformat()}"] = f"{type(e).__name__}: {e}"
                        log.warning(
                            "whoop_private.trend.metric_failed",
                            metric=metric,
                            anchor=anchor.isoformat(),
                            error=str(e),
                        )
                        continue
                    if not payload:
                        continue
                    fetched += 1
                    slim = transforms.slim_trend_payload(payload, metric, anchor)
                    upsert_rows(
                        "raw_whoop_trend",
                        [{"metric": metric, "end_date": anchor, "payload": Jsonb(slim)}],
                        conflict_cols=["metric", "end_date"],
                        update_cols=["payload", "fetched_at"],
                        connection=c,
                    )
                    rid = _raw_id(c, "raw_whoop_trend", {"metric": metric, "end_date": anchor})
                    for row in transforms.transform_trend_points(payload, metric, anchor):
                        key = (row["day"], row["metric"])
                        if key in fact_by_day:
                            continue  # an earlier (fresher) anchor already set it
                        row["raw_id"] = rid
                        row["updated_at"] = _now()
                        fact_by_day[key] = row
            if fact_by_day:
                upsert_rows(
                    "fact_whoop_metric_daily",
                    list(fact_by_day.values()),
                    conflict_cols=["day", "metric"],
                    connection=c,
                )
        run.fetched(fetched)
        run.upserted(len(fact_by_day))
        if errors:
            run.add_metadata(errors=errors)
        return len(fact_by_day)


def ingest_sleep_need(client: WhoopPrivateClient) -> int:
    """Snapshot today's sleep-need breakdown into fact_whoop_sleep_need."""
    day = _local_today()
    with ingestion_run("whoop_private", "sleep_need", day=day.isoformat()) as run:
        payload = client.sleep_need()
        run.fetched(1 if payload else 0)
        row = transforms.transform_sleep_need(payload, day)
        if row is None:
            return 0
        with tx() as c:
            upsert_rows(
                "raw_whoop_sleep_need",
                [{"day": day, "payload": Jsonb(payload)}],
                conflict_cols=["day"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            row["raw_id"] = _raw_id(c, "raw_whoop_sleep_need", {"day": day})
            row["updated_at"] = _now()
            upsert_rows("fact_whoop_sleep_need", [row], conflict_cols=["day"], connection=c)
        run.upserted(1)
        return 1


def ingest_behavior_impact(client: WhoopPrivateClient) -> int:
    """Snapshot today's recovery-impact analysis into fact_whoop_behavior_impact."""
    captured_on = _local_today()
    with ingestion_run(
        "whoop_private", "behavior_impact", captured_on=captured_on.isoformat()
    ) as run:
        payload = client.behavior_impact()
        run.fetched(1 if payload else 0)
        rows = transforms.transform_behavior_impact(payload, captured_on)
        if not rows:
            return 0
        with tx() as c:
            upsert_rows(
                "raw_whoop_behavior_impact",
                [{"captured_on": captured_on, "payload": Jsonb(payload)}],
                conflict_cols=["captured_on"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            rid = _raw_id(c, "raw_whoop_behavior_impact", {"captured_on": captured_on})
            for row in rows:
                row["raw_id"] = rid
                row["updated_at"] = _now()
            upsert_rows(
                "fact_whoop_behavior_impact",
                rows,
                conflict_cols=["captured_on", "impact_uuid", "outcome"],
                connection=c,
            )
        run.upserted(len(rows))
        return len(rows)


def ingest_lifts(client: WhoopPrivateClient, *, backfill_days: int | None = None) -> int:
    """Pull exact per-set Strength Trainer detail. Enumerates strength workouts
    from fact_workout (the public-OAuth ingester already lands every activity),
    fetches each workout's cardio-details breakdown, and upserts
    fact_whoop_lift_workout (aggregate) + fact_whoop_lift_set (one row per set).
    Incremental covers the last 7 days; backfill covers `backfill_days`."""
    cutoff = _local_today() - timedelta(days=backfill_days if backfill_days else 7)
    with ingestion_run("whoop_private", "lift", since=cutoff.isoformat()) as run:
        with tx() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT workout_id, day, strain,
                       EXTRACT(EPOCH FROM (end_ts - start_ts)) / 60.0 AS dur_min
                FROM fact_workout
                WHERE (sport_name ILIKE %s OR sport_name ILIKE %s)
                  AND day >= %s
                ORDER BY day
                """,
                ["%weight%", "%strength%", cutoff],
            )
            workouts = cur.fetchall()

        fetched = 0
        errors: dict[str, str] = {}
        parsed: list[tuple[str, dict, list[dict], dict]] = []  # (aid, wk_row, set_rows, slim_raw)
        for w in workouts:
            aid = str(w["workout_id"])  # fact_workout.workout_id is uuid; our tables key on text
            try:
                payload = client.cardio_details(aid)
            except WhoopAuthExpired:
                raise
            except Exception as e:
                errors[aid] = f"{type(e).__name__}: {e}"
                continue
            wcd = (payload or {}).get("weightlifting_cardio_details")
            if not wcd:
                continue  # not a strength workout, or no breakdown
            fetched += 1
            wk_row, set_rows = transforms.transform_cardio_details(payload, aid, w["day"])
            if wk_row is None:
                continue
            wk_row["strain"] = _safe_num(w["strain"])
            wk_row["duration_minutes"] = round(w["dur_min"], 1) if w["dur_min"] else None
            parsed.append((aid, wk_row, set_rows, {"weightlifting_cardio_details": wcd}))

        total_sets = 0
        now = _now()
        with tx() as c:
            for aid, wk_row, set_rows, slim in parsed:
                upsert_rows(
                    "raw_whoop_lift",
                    [{"activity_id": aid, "payload": Jsonb(slim)}],
                    conflict_cols=["activity_id"],
                    update_cols=["payload", "fetched_at"],
                    connection=c,
                )
                rid = _raw_id(c, "raw_whoop_lift", {"activity_id": aid})
                wk_row["exercises"] = Jsonb(wk_row["exercises"])
                wk_row["raw_id"] = rid
                wk_row["updated_at"] = now
                upsert_rows(
                    "fact_whoop_lift_workout", [wk_row],
                    conflict_cols=["activity_id"], connection=c,
                )
                for s in set_rows:
                    s["raw_id"] = rid
                    s["updated_at"] = now
                if set_rows:
                    upsert_rows(
                        "fact_whoop_lift_set", set_rows,
                        conflict_cols=["activity_id", "exercise_id", "set_index"],
                        connection=c,
                    )
                total_sets += len(set_rows)

        run.fetched(fetched)
        run.upserted(total_sets)
        run.add_metadata(workouts=len(parsed), sets=total_sets)
        if errors:
            run.add_metadata(errors=errors)
        return total_sets


# ---- orchestration ---------------------------------------------------------
def _safe_num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def run_all(*, backfill_days: int | None = None, data_type: str | None = None) -> dict:
    """Run every pipeline, returning per-type counts. Failures in one pipeline
    don't abort the others — each is its own ingestion_run."""
    results: dict[str, int | str] = {}
    pipelines: list[tuple[str, Callable[[WhoopPrivateClient], int]]] = [
        ("trend", lambda c: ingest_trends(c, backfill_days=backfill_days)),
        ("sleep_need", lambda c: ingest_sleep_need(c)),
        ("behavior_impact", lambda c: ingest_behavior_impact(c)),
        ("lift", lambda c: ingest_lifts(c, backfill_days=backfill_days)),
    ]
    if data_type:
        pipelines = [(n, f) for n, f in pipelines if n == data_type]
        if not pipelines:
            return {"error": f"unknown data_type: {data_type}"}

    auth = WhoopAuth()
    auth.ensure_fresh()  # fail fast before opening any runs
    with WhoopPrivateClient(auth=auth) as client:
        for name, fn in pipelines:
            try:
                results[name] = fn(client)
            except Exception as e:
                log.exception("whoop_private.pipeline.failed", pipeline=name)
                results[name] = f"FAILED: {type(e).__name__}: {e}"
    return results
