"""Zero -> canonical facts.

Zero is an Apple Health passthrough for everything except fasts: its weight,
RHR, sleep and active-minute rows are copies of whatever HealthKit held.
That makes it low-precedence for those domains (see dim_source_priority) but
it is the *only* source before 2023-11-16, so it carries the earliest 15
months of body-weight history in the warehouse.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from lifeos_core.logging import get_logger
from unify.common import jsonb, local_day, num, uid, upsert

log = get_logger(__name__)

SOURCE = "zero"


def _rows(conn: psycopg.Connection, entity: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, natural_key, occurred_on, payload FROM raw_import "
            "WHERE source = %s AND entity = %s",
            [SOURCE, entity],
        )
        return cur.fetchall()


def _ts(v: str | None) -> datetime | None:
    if not v or str(v)[:4] in ("", "0001"):
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def project_weight(conn: psycopg.Connection) -> int:
    rows = []
    for r in _rows(conn, "weight"):
        w = r["payload"]
        ts = _ts(w.get("log_dtm"))
        kg = num(w.get("weightKG"))
        if ts is None or not kg:
            continue
        rows.append({
            "measurement_uid": uid(SOURCE, "scale", w.get("id")),
            "source": SOURCE,
            "method": "scale",
            "measured_at": ts,
            "day": local_day(ts),
            "weight_kg": kg,
            "body_fat_pct": None, "lean_mass_kg": None, "fat_mass_kg": None,
            "bone_mineral_kg": None, "visceral_fat": None, "bmi": None,
            "muscle_mass_kg": None, "body_water_pct": None,
            "region": jsonb({}),
            "confidence": 0.55,
            "raw_id": r["id"], "payload": jsonb({}),
            "updated_at": datetime.now(UTC),
        })
    n = upsert(conn, "fact_body_composition", rows, conflict=["measurement_uid"])
    log.info("zero.project.weight", rows=n)
    return n


def project_rhr(conn: psycopg.Connection) -> int:
    """Zero logs many RHR rows per day, and writes 0 when the reading is
    missing. Keep the daily mean of the non-zero readings."""
    by_day: dict = {}
    for r in _rows(conn, "resting_hr"):
        v = num(r["payload"].get("restingHRBPM"))
        ts = _ts(r["payload"].get("log_dtm"))
        if not v or v <= 0 or ts is None:
            continue
        by_day.setdefault(local_day(ts), []).append(v)
    rows = [{
        "day": day, "metric": "resting_hr", "source": SOURCE,
        "value": round(sum(vals) / len(vals), 1), "unit": "bpm",
        "payload": jsonb({"n": len(vals)}),
        "updated_at": datetime.now(UTC),
    } for day, vals in by_day.items()]
    n = upsert(conn, "fact_daily_metric", rows, conflict=["day", "metric", "source"])
    log.info("zero.project.rhr", rows=n)
    return n


def project_sleep(conn: psycopg.Connection) -> int:
    rows = []
    for r in _rows(conn, "sleep"):
        s = r["payload"]
        start, end = _ts(s.get("SleepStartDTM")), _ts(s.get("SleepEndDTM"))
        if not start or not end:
            continue
        dur = num(s.get("DurationInSeconds")) or (end - start).total_seconds()
        rows.append({
            "sleep_uid": uid(SOURCE, s.get("ID")),
            "source": SOURCE,
            "source_sleep_id": str(s.get("ID")),
            "start_ts": start,
            "end_ts": end,
            "day": local_day(end),
            # Apple Health writes short daytime blocks as separate records;
            # anything under 3h is a nap, not a night.
            "is_nap": bool(dur and dur < 3 * 3600),
            "time_in_bed_s": dur,
            "asleep_s": dur,
            "awake_s": None, "light_s": None, "deep_s": None, "rem_s": None,
            "unmeasurable_s": None, "efficiency_pct": None,
            "performance_pct": None, "consistency_pct": None, "score": None,
            "sleep_need_s": None, "sleep_debt_s": None,
            "respiratory_rate": None, "avg_hr": None, "avg_spo2": None,
            "lowest_spo2": None, "avg_stress": None,
            "disturbance_count": None, "cycle_count": None,
            "restless_moments": None,
            "raw_id": r["id"],
            "payload": jsonb({"DataSource": s.get("DataSource")}),
            "updated_at": datetime.now(UTC),
        })
    n = upsert(conn, "fact_sleep_session", rows, conflict=["sleep_uid"])
    log.info("zero.project.sleep", rows=n)
    return n


def project_fasts(conn: psycopg.Connection) -> int:
    rows = []
    for r in _rows(conn, "fast"):
        f = r["payload"]
        start, end = _ts(f.get("StartDTM")), _ts(f.get("EndDTM"))
        if not start:
            continue
        dur = (end - start).total_seconds() if end else None
        goal = num(f.get("GoalHours"))
        rows.append({
            "fast_uid": uid(SOURCE, f.get("FastID")),
            "source": SOURCE,
            "start_ts": start,
            "end_ts": end,
            "day": local_day(start),
            "duration_s": dur,
            "goal_hours": goal,
            "goal_key": f.get("GoalID"),
            "is_ended": bool(f.get("IsEnded")),
            "hit_goal": (dur / 3600 >= goal) if (dur and goal) else None,
            "raw_id": r["id"], "payload": jsonb({}),
            "updated_at": datetime.now(UTC),
        })
    n = upsert(conn, "fact_fast", rows, conflict=["fast_uid"])
    log.info("zero.project.fast", rows=n)
    return n


def project_active_minutes(conn: psycopg.Connection) -> dict[str, int]:
    """Apple activity blocks -> daily active-minutes and intensity metrics.

    These are deliberately NOT written to fact_activity. They are Apple's
    move-ring segments, not sessions: a single day routinely has half a dozen
    overlapping blocks with no name beyond "Activity". Treating them as
    workouts both inflates the session count that vw_adherence depends on and
    drags unrelated real sessions into their clusters. The information that
    matters -- how much the day moved -- survives as a daily metric.
    """
    totals: dict = {}
    high: dict = {}
    for r in _rows(conn, "active_minutes"):
        a = r["payload"]
        start = _ts(a.get("StartDTM"))
        total = num(a.get("TotalActiveMins")) or 0
        if start is None or total <= 0:
            continue
        day = local_day(start)
        totals[day] = totals.get(day, 0) + total
        high[day] = high.get(day, 0) + (num(a.get("HighMins")) or 0)

    now = datetime.now(UTC)
    rows = [{
        "day": day, "metric": "active_minutes", "source": SOURCE,
        "value": round(mins, 1), "unit": "min", "payload": jsonb({}),
        "updated_at": now,
    } for day, mins in totals.items()]
    rows += [{
        "day": day, "metric": "high_intensity_minutes", "source": SOURCE,
        "value": round(mins, 1), "unit": "min", "payload": jsonb({}),
        "updated_at": now,
    } for day, mins in high.items() if mins > 0]

    n_m = upsert(conn, "fact_daily_metric", rows,
                 conflict=["day", "metric", "source"])
    log.info("zero.project.active_minutes", metrics=n_m)
    return {"metrics": n_m}


def project_all(conn: psycopg.Connection) -> dict:
    out = {
        "weight": project_weight(conn),
        "resting_hr": project_rhr(conn),
        "sleep": project_sleep(conn),
        "fasts": project_fasts(conn),
    }
    out.update({f"active_{k}": v for k, v in project_active_minutes(conn).items()})
    # caloric_intake_data is deliberately skipped: every timestamp in the
    # export is 0001-01-01, so the rows cannot be placed on a calendar.
    return out
