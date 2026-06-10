"""One-off backfill: load Whoop private-API raw payloads from JSON files into
the warehouse, reusing ingest_whoop_private.transforms.

This is the documented fallback path (see the optimization plan) for when the
native ingester can't run live — e.g. the oauth_tokens(service='whoop_private')
bearer is stale, but the data was captured another way (the Whoop MCP's
whoop_raw escape hatch). Each file holds one endpoint response, either the raw
gateway body or the MCP envelope {path, method, status, response}. The script
sniffs the shape, routes to the right transform, and upserts the same tables the
native ingester writes — so a later native `--backfill` run dedups cleanly on
top of it.

Usage:
    python -m scripts.backfill_whoop_private_from_files <dir-with-*.json> [--end-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from ingest_whoop_private import transforms
from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

_TREND_SEG_KEYS = ("week_time_segment", "month_time_segment", "six_month_time_segment")


def _inner(doc: dict) -> dict:
    """Unwrap the MCP whoop_raw envelope if present."""
    if isinstance(doc, dict) and "response" in doc and "path" in doc:
        return doc.get("response") or {}
    return doc


def _trend_metric(resp: dict) -> str | None:
    for k in _TREND_SEG_KEYS:
        seg = resp.get(k)
        if isinstance(seg, dict):
            metrics = seg.get("metrics") or []
            if metrics and isinstance(metrics[0], dict) and metrics[0].get("trend_key"):
                return metrics[0]["trend_key"]
    return None


def _raw_id(c, table: str, where: dict) -> int | None:
    clause = " AND ".join(f"{k} = %s" for k in where)
    with c.cursor() as cur:
        cur.execute(
            f"SELECT id FROM {table} WHERE {clause} ORDER BY id DESC LIMIT 1",
            list(where.values()),
        )
        row = cur.fetchone()
        return row["id"] if row else None


def _load_trend(c, resp: dict, end_date: date, metric: str | None = None) -> int:
    # Prefer the caller-supplied metric (the requested name, e.g. from the
    # filename) over the payload's trend_key — Whoop's trend_key diverges from
    # the request path for several metrics (SLEEP_DEBT_POST -> SLEEP_NEED_...,
    # STRESS -> HIGH_STRESS, RESTORATIVE_SLEEP -> REM_SLEEP), and the native
    # ingester + mart_daily key off the requested name.
    metric = metric or _trend_metric(resp)
    if not metric:
        return 0
    slim = transforms.slim_trend_payload(resp, metric, end_date)
    upsert_rows(
        "raw_whoop_trend",
        [{"metric": metric, "end_date": end_date, "payload": Jsonb(slim)}],
        conflict_cols=["metric", "end_date"],
        update_cols=["payload", "fetched_at"],
        connection=c,
    )
    rid = _raw_id(c, "raw_whoop_trend", {"metric": metric, "end_date": end_date})
    rows = transforms.transform_trend_points(resp, metric, end_date)
    now = datetime.now(UTC)
    for r in rows:
        r["raw_id"] = rid
        r["updated_at"] = now
    if rows:
        upsert_rows(
            "fact_whoop_metric_daily", rows, conflict_cols=["day", "metric"], connection=c
        )
    log.info("backfill.trend", metric=metric, days=len(rows))
    return len(rows)


def _load_sleep_need(c, resp: dict, day: date) -> int:
    row = transforms.transform_sleep_need(resp, day)
    if row is None:
        return 0
    upsert_rows(
        "raw_whoop_sleep_need",
        [{"day": day, "payload": Jsonb(resp)}],
        conflict_cols=["day"],
        update_cols=["payload", "fetched_at"],
        connection=c,
    )
    row["raw_id"] = _raw_id(c, "raw_whoop_sleep_need", {"day": day})
    row["updated_at"] = datetime.now(UTC)
    upsert_rows("fact_whoop_sleep_need", [row], conflict_cols=["day"], connection=c)
    log.info("backfill.sleep_need", day=day.isoformat())
    return 1


def _load_behavior_impact(c, resp: dict, day: date) -> int:
    rows = transforms.transform_behavior_impact(resp, day)
    if not rows:
        return 0
    upsert_rows(
        "raw_whoop_behavior_impact",
        [{"captured_on": day, "payload": Jsonb(resp)}],
        conflict_cols=["captured_on"],
        update_cols=["payload", "fetched_at"],
        connection=c,
    )
    rid = _raw_id(c, "raw_whoop_behavior_impact", {"captured_on": day})
    now = datetime.now(UTC)
    for r in rows:
        r["raw_id"] = rid
        r["updated_at"] = now
    upsert_rows(
        "fact_whoop_behavior_impact",
        rows,
        conflict_cols=["captured_on", "impact_uuid", "outcome"],
        connection=c,
    )
    log.info("backfill.behavior_impact", day=day.isoformat(), rows=len(rows))
    return len(rows)


def _is_lift_records(resp) -> bool:
    """A whoop_lift_history payload is a JSON array of workout records, each
    carrying an activity_id + msk_total_volume_kg."""
    return (
        isinstance(resp, list)
        and bool(resp)
        and isinstance(resp[0], dict)
        and "activity_id" in resp[0]
        and "msk_total_volume_kg" in resp[0]
    )


def _load_lifts(c, records: list) -> int:
    written = 0
    for rec in records:
        row = transforms.transform_lift_workout(rec)
        if row is None:
            continue
        exercises = row.pop("exercises")
        upsert_rows(
            "raw_whoop_lift",
            [{"activity_id": row["activity_id"], "payload": Jsonb(rec)}],
            conflict_cols=["activity_id"],
            update_cols=["payload", "fetched_at"],
            connection=c,
        )
        row["raw_id"] = _raw_id(c, "raw_whoop_lift", {"activity_id": row["activity_id"]})
        row["exercises"] = Jsonb(exercises)
        row["updated_at"] = datetime.now(UTC)
        upsert_rows(
            "fact_whoop_lift_workout", [row], conflict_cols=["activity_id"], connection=c
        )
        written += 1
    log.info("backfill.lifts", workouts=written)
    return written


def run(directory: Path, end_date: date) -> dict:
    summary: dict[str, int] = {
        "trend_metrics": 0, "trend_days": 0, "sleep_need": 0,
        "behavior_impact": 0, "lift_workouts": 0,
    }
    files = sorted(directory.glob("*.json"))
    if not files:
        log.warning("backfill.no_files", dir=str(directory))
        return summary
    with tx() as c:
        for f in files:
            try:
                resp = _inner(json.loads(f.read_text()))
            except Exception as e:
                log.warning("backfill.unreadable", file=f.name, error=str(e))
                continue
            if _is_lift_records(resp):
                summary["lift_workouts"] += _load_lifts(c, resp)
                continue
            if not isinstance(resp, dict) or not resp:
                continue
            if _trend_metric(resp):
                # The filename stem is the requested metric (e.g. STEPS.json);
                # prefer it over the payload's trend_key for canonical naming.
                n = _load_trend(c, resp, end_date, metric=f.stem.upper())
                summary["trend_metrics"] += 1 if n else 0
                summary["trend_days"] += n
            elif "need_breakdown" in resp:
                summary["sleep_need"] += _load_sleep_need(c, resp, end_date)
            elif "tiles" in resp:
                summary["behavior_impact"] += _load_behavior_impact(c, resp, end_date)
            else:
                log.warning("backfill.unrecognized", file=f.name, keys=list(resp.keys())[:8])
    return summary


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="backfill_whoop_private_from_files")
    p.add_argument("directory", type=Path, help="Directory of *.json payload files.")
    p.add_argument(
        "--end-date", type=str, default=date.today().isoformat(),
        help="end_date / snapshot day to stamp these payloads with (YYYY-MM-DD).",
    )
    args = p.parse_args(argv)
    out = run(args.directory, date.fromisoformat(args.end_date))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
