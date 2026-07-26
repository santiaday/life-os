"""Whoop -> canonical facts.

Three Whoop streams land in this warehouse and all three feed the unified
layer:

  source='whoop'         public developer OAuth API. Richest per-session data:
                         HR, kilojoules, and the six-bucket zone split.
  source='whoop_lift'    private Strength Trainer feed. The only Whoop stream
                         with true per-set reps and loads.
  source='whoop_export'  the official CSV export. Historical only, but it is
                         the ONLY source covering 2023-11-16 -> 2025-08-23,
                         and it carries HR zone percentages for that window.

Where they overlap, `unify.dedupe` keeps one row per real session. Precedence
is set in dim_source_priority, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from lifeos_core.logging import get_logger
from unify import taxonomy
from unify.common import (
    hash_uid,
    jsonb,
    lb_to_kg,
    local_day,
    num,
    parse_offset,
    parse_ts,
    resolve_and_register,
    uid,
    upsert,
)

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _min_to_s(v) -> float | None:
    """Minutes -> seconds. Whoop's public API and CSV export both report sleep
    and zone durations in minutes; the canonical tables are seconds."""
    return float(v) * 60 if v is not None else None


# ---------------------------------------------------------------------------
# Public OAuth API: fact_workout -> fact_activity
# ---------------------------------------------------------------------------

def project_public_workouts(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_workout")
        raw = cur.fetchall()
    rows = []
    for w in raw:
        vendor = w["sport_name"] or taxonomy.WHOOP_SPORT_ID_NAMES.get(w["sport_id"] or -1, "other")
        ctype, is_res, _ = taxonomy.canonical_activity_type("whoop", vendor)
        dur = (w["end_ts"] - w["start_ts"]).total_seconds() if w["end_ts"] else None
        rows.append({
            "activity_uid": uid("whoop", w["workout_id"]),
            "source": "whoop",
            "source_activity_id": str(w["workout_id"]),
            "start_ts": w["start_ts"],
            "end_ts": w["end_ts"],
            "day": w["day"] or local_day(w["start_ts"]),
            "duration_s": dur,
            "moving_duration_s": None,
            "activity_type": ctype,
            "vendor_type": vendor,
            "name": vendor,
            "is_resistance": is_res,
            "strain": w["strain"],
            "training_load": None, "aerobic_te": None, "anaerobic_te": None,
            "rpe": None, "intensity_pct": None,
            "avg_hr": w["avg_heart_rate"],
            "max_hr": w["max_heart_rate"],
            "min_hr": None,
            "kcal": (float(w["kilojoules"]) / 4.184) if w["kilojoules"] else None,
            "kilojoules": w["kilojoules"],
            "zone_0_s": _min_to_s(w["zone_zero_min"]), "zone_1_s": _min_to_s(w["zone_one_min"]),
            "zone_2_s": _min_to_s(w["zone_two_min"]), "zone_3_s": _min_to_s(w["zone_three_min"]),
            "zone_4_s": _min_to_s(w["zone_four_min"]), "zone_5_s": _min_to_s(w["zone_five_min"]),
            "distance_m": w["distance_meters"],
            "elevation_gain_m": w["altitude_gain_meters"],
            "steps": None,
            "total_sets": None, "total_reps": None, "total_volume_kg": None,
            "unique_exercises": None,
            "raw_id": None,
            "payload": jsonb({"sport_id": w["sport_id"]}),
            "updated_at": _now(),
        })
    n = upsert(conn, "fact_activity", rows, conflict=["activity_uid"])
    log.info("whoop.project.workout", rows=n)
    return n


# ---------------------------------------------------------------------------
# Private Strength Trainer: per-set detail
# ---------------------------------------------------------------------------

def project_lifts(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_whoop_lift_workout")
        workouts = cur.fetchall()
        cur.execute("SELECT * FROM fact_whoop_lift_set ORDER BY activity_id, exercise_id, set_index")
        sets = cur.fetchall()

    by_activity: dict[str, list[dict]] = {}
    for s in sets:
        by_activity.setdefault(s["activity_id"], []).append(s)

    act_rows, ex_rows, set_rows = [], [], []
    for w in workouts:
        aid = w["activity_id"]
        auid = uid("whoop_lift", aid)
        mysets = by_activity.get(aid, [])
        start = datetime.combine(w["day"], datetime.min.time(), tzinfo=UTC)
        dur_s = float(w["duration_minutes"]) * 60 if w["duration_minutes"] else None

        # The lift feed has no start timestamp of its own -- borrow it from the
        # matching fact_workout row when one exists so dedupe can align them.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT start_ts, end_ts FROM fact_workout "
                "WHERE workout_id::text = %s LIMIT 1", [aid])
            hit = cur.fetchone()
        if hit:
            start = hit["start_ts"]
            end = hit["end_ts"]
        else:
            end = start + timedelta(seconds=dur_s) if dur_s else None

        act_rows.append({
            "activity_uid": auid,
            "source": "whoop_lift",
            "source_activity_id": aid,
            "start_ts": start,
            "end_ts": end,
            "day": w["day"],
            "duration_s": dur_s,
            "moving_duration_s": None,
            "activity_type": "strength",
            "vendor_type": "strength trainer",
            "name": w["name"] or "Strength Trainer",
            "is_resistance": True,
            "strain": w["strain"],
            "training_load": None, "aerobic_te": None, "anaerobic_te": None,
            "rpe": None,
            "intensity_pct": w["intensity_pct"],
            "avg_hr": None, "max_hr": None, "min_hr": None,
            "kcal": None, "kilojoules": None,
            "zone_0_s": None, "zone_1_s": None, "zone_2_s": None,
            "zone_3_s": None, "zone_4_s": None, "zone_5_s": None,
            "distance_m": None, "elevation_gain_m": None, "steps": None,
            "total_sets": w["set_count"],
            "total_reps": sum(s["reps"] or 0 for s in mysets) or None,
            "total_volume_kg": w["total_volume_kg"],
            "unique_exercises": w["exercise_count"],
            "raw_id": None,
            "payload": jsonb({"exercises": w["exercises"]}),
            "updated_at": _now(),
        })

        # Group sets by exercise, preserving performed order.
        order: list[str] = []
        grouped: dict[str, list[dict]] = {}
        for s in mysets:
            if s["exercise_id"] not in grouped:
                order.append(s["exercise_id"])
                grouped[s["exercise_id"]] = []
            grouped[s["exercise_id"]].append(s)

        for ei, ex_id in enumerate(order):
            group = grouped[ex_id]
            label = group[0]["exercise_name"] or ex_id
            key = resolve_and_register(conn, "whoop", label, vendor_id=ex_id)
            vols = [(s["reps"] or 0) * float(s["weight_kg"] or 0) for s in group]
            weights = [float(s["weight_kg"]) for s in group if s["weight_kg"]]
            ex_rows.append({
                "activity_uid": auid,
                "exercise_index": ei,
                "exercise_key": key,
                "vendor_exercise": label,
                "vendor_category": None,
                "source": "whoop_lift",
                "day": w["day"],
                "granularity": "set",
                "set_count": len(group),
                "total_reps": sum(s["reps"] or 0 for s in group) or None,
                "total_volume_kg": sum(vols) or None,
                "max_weight_kg": max(weights) if weights else None,
                "duration_s": sum(s["time_seconds"] or 0 for s in group) or None,
                "is_pr": any(s["is_pr"] for s in group),
                "updated_at": _now(),
            })
            for si, s in enumerate(group):
                set_rows.append({
                    "activity_uid": auid,
                    "exercise_index": ei,
                    "set_index": si,
                    "exercise_key": key,
                    "vendor_exercise": label,
                    "source": "whoop_lift",
                    "day": w["day"],
                    "set_type": s["volume_type"],
                    "reps": s["reps"],
                    "weight_kg": s["weight_kg"],
                    "duration_s": s["time_seconds"],
                    "distance_m": None,
                    "rpe": None,
                    "avg_hr": s["avg_hr"],
                    "is_pr": s["is_pr"],
                    "volume_kg": (s["reps"] or 0) * float(s["weight_kg"] or 0) or None,
                    "updated_at": _now(),
                })

    n_a = upsert(conn, "fact_activity", act_rows, conflict=["activity_uid"])
    n_e = upsert(conn, "fact_activity_exercise", ex_rows,
                 conflict=["activity_uid", "exercise_index"])
    n_s = upsert(conn, "fact_activity_set", set_rows,
                 conflict=["activity_uid", "exercise_index", "set_index"])
    log.info("whoop.project.lift", activities=n_a, exercises=n_e, sets=n_s)
    return {"activities": n_a, "exercises": n_e, "sets": n_s}


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

def project_public_sleep(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_sleep")
        raw = cur.fetchall()
    rows = []
    for s in raw:
        in_bed = _min_to_s(s["total_in_bed_min"])
        awake = _min_to_s(s["total_awake_min"])
        asleep = (in_bed - awake) if (in_bed is not None and awake is not None) else None
        rows.append({
            "sleep_uid": uid("whoop", s["sleep_id"]),
            "source": "whoop",
            "source_sleep_id": str(s["sleep_id"]),
            "start_ts": s["start_ts"],
            "end_ts": s["end_ts"],
            "day": s["day"] or local_day(s["end_ts"]),
            "is_nap": s["is_nap"],
            "time_in_bed_s": in_bed,
            "asleep_s": asleep,
            "awake_s": awake,
            "light_s": _min_to_s(s["total_light_min"]),
            "deep_s": _min_to_s(s["total_slow_wave_min"]),
            "rem_s": _min_to_s(s["total_rem_min"]),
            "unmeasurable_s": None,
            "efficiency_pct": s["sleep_efficiency_pct"],
            "performance_pct": s["sleep_performance_pct"],
            "consistency_pct": s["sleep_consistency_pct"],
            "score": s["sleep_performance_pct"],
            "sleep_need_s": None, "sleep_debt_s": None,
            "respiratory_rate": None, "avg_hr": None,
            "avg_spo2": None, "lowest_spo2": None, "avg_stress": None,
            "disturbance_count": s["disturbance_count"],
            "cycle_count": s["sleep_cycle_count"],
            "restless_moments": None,
            "raw_id": None, "payload": jsonb({}),
            "updated_at": _now(),
        })
    n = upsert(conn, "fact_sleep_session", rows, conflict=["sleep_uid"])
    log.info("whoop.project.sleep", rows=n)
    return n


# ---------------------------------------------------------------------------
# Daily metrics + body composition from the private trends feed
# ---------------------------------------------------------------------------

# fact_whoop_metric_daily.metric -> (canonical name, unit, scale)
_TREND_METRICS = {
    "STEPS":                    ("steps", "count", 1),
    "CALORIES":                 ("calories_total", "kcal", 1),
    "DAY_STRAIN":               ("day_strain", "score", 1),
    "RHR":                      ("resting_hr", "bpm", 1),
    "HRV":                      ("hrv", "ms", 1),
    "RECOVERY":                 ("recovery_score", "pct", 1),
    "RESPIRATORY_RATE":         ("respiratory_rate", "rpm", 1),
    "VO2_MAX":                  ("vo2_max", "ml/kg/min", 1),
    "SLEEP_PERFORMANCE":        ("sleep_performance", "pct", 1),
    "SLEEP_EFFICIENCY":         ("sleep_efficiency", "pct", 1),
    "SLEEP_CONSISTENCY":        ("sleep_consistency", "pct", 1),
    "SLEEP_DEBT_POST":          ("sleep_debt", "min", 1),
    "RESTORATIVE_SLEEP":        ("restorative_sleep", "min", 1),
    "TIME_IN_BED":              ("time_in_bed", "min", 1),
    "STRESS":                   ("stress_avg", "score", 1),
    "STRESS_DURING_SLEEP":      ("stress_sleep", "score", 1),
    "STRESS_DURING_NON_STRAIN": ("stress_non_strain", "score", 1),
    "AVERAGE_HR":               ("avg_hr_day", "bpm", 1),
    "HR_ZONES_1_3":             ("hr_zone_1_3_time", "min", 1),
    "HR_ZONES_4_5":             ("hr_zone_4_5_time", "min", 1),
    "STRENGTH_ACTIVITY_TIME":   ("strength_activity_time", "min", 1),
}


def project_trends(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT day, metric, value, unit FROM fact_whoop_metric_daily")
        raw = cur.fetchall()

    metric_rows, body_rows = [], []
    weights: dict = {}
    bodyfat: dict = {}
    for r in raw:
        if r["metric"] == "WEIGHT":
            weights[r["day"]] = num(r["value"])
            continue
        if r["metric"] == "BODY_COMPOSITION":
            # Whoop Body reports LEAN MASS percentage under this name, not body
            # fat. Taking it at face value produced "81% body fat" rows and a
            # matching 15 kg lean-mass figure -- both physiologically absurd,
            # and both flagged by unify.quality before this was caught.
            lean_pct = num(r["value"])
            bodyfat[r["day"]] = (100.0 - lean_pct) if lean_pct is not None else None
            continue
        spec = _TREND_METRICS.get(r["metric"])
        if not spec or r["value"] is None:
            continue
        name, unit, scale = spec
        metric_rows.append({"day": r["day"], "metric": name, "source": "whoop",
                            "value": float(r["value"]) * scale, "unit": unit,
                            "payload": jsonb({}), "updated_at": _now()})

    # Whoop reports WEIGHT in pounds in the trends feed.
    for day in set(weights) | set(bodyfat):
        w_lb = weights.get(day)
        w_kg = lb_to_kg(w_lb) if w_lb else None
        bf = bodyfat.get(day)
        ts = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        body_rows.append({
            "measurement_uid": uid("whoop", "bioimpedance", int(ts.timestamp())),
            "source": "whoop",
            "method": "bioimpedance",
            "measured_at": ts,
            "day": day,
            "weight_kg": w_kg,
            "body_fat_pct": bf,
            "lean_mass_kg": (w_kg * (1 - bf / 100)) if (w_kg and bf) else None,
            "fat_mass_kg": (w_kg * bf / 100) if (w_kg and bf) else None,
            "bone_mineral_kg": None, "visceral_fat": None, "bmi": None,
            "muscle_mass_kg": None, "body_water_pct": None,
            "region": jsonb({}),
            "confidence": 0.4,
            "raw_id": None, "payload": jsonb({"weight_lb": w_lb}),
            "updated_at": _now(),
        })

    # Cycle-level strain / kJ that the trends feed doesn't carry.
    with conn.cursor() as cur:
        cur.execute("SELECT day, scaled_strain, day_kilojoules, avg_heart_rate, "
                    "max_heart_rate FROM fact_cycle WHERE day IS NOT NULL")
        for c in cur.fetchall():
            for col, name, unit in (("scaled_strain", "day_strain", "score"),
                                    ("day_kilojoules", "day_kilojoules", "kJ"),
                                    ("avg_heart_rate", "avg_hr_day", "bpm"),
                                    ("max_heart_rate", "max_hr_day", "bpm")):
                if c[col] is not None:
                    metric_rows.append({"day": c["day"], "metric": name, "source": "whoop",
                                        "value": float(c[col]), "unit": unit,
                                        "payload": jsonb({}), "updated_at": _now()})
        cur.execute("SELECT day, recovery_score, hrv_rmssd_ms, resting_heart_rate, "
                    "spo2_percentage, skin_temp_celsius FROM fact_recovery")
        for c in cur.fetchall():
            for col, name, unit in (("recovery_score", "recovery_score", "pct"),
                                    ("hrv_rmssd_ms", "hrv", "ms"),
                                    ("resting_heart_rate", "resting_hr", "bpm"),
                                    ("spo2_percentage", "spo2_avg", "pct"),
                                    ("skin_temp_celsius", "skin_temp", "C")):
                if c[col] is not None:
                    metric_rows.append({"day": c["day"], "metric": name, "source": "whoop",
                                        "value": float(c[col]), "unit": unit,
                                        "payload": jsonb({}), "updated_at": _now()})

    dedup = {(r["day"], r["metric"], r["source"]): r for r in metric_rows}
    n_m = upsert(conn, "fact_daily_metric", list(dedup.values()),
                 conflict=["day", "metric", "source"])
    n_b = upsert(conn, "fact_body_composition", body_rows, conflict=["measurement_uid"])
    log.info("whoop.project.trends", metrics=n_m, body=n_b)
    return {"metrics": n_m, "body": n_b}


# ---------------------------------------------------------------------------
# The official CSV export -- the only source for 2023-11-16 .. 2025-08-23
# ---------------------------------------------------------------------------

def project_export_workouts(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id, natural_key, occurred_on, payload FROM raw_import "
                    "WHERE source = 'whoop_export' AND entity = 'workout'")
        raw = cur.fetchall()
    rows = []
    for r in raw:
        w = r["payload"]
        tz = parse_offset(w.get("Cycle timezone"))
        start = parse_ts(w.get("Workout start time"), tz)
        end = parse_ts(w.get("Workout end time"), tz)
        if not start:
            continue
        dur_min = num(w.get("Duration (min)"))
        dur_s = dur_min * 60 if dur_min else (
            (end - start).total_seconds() if end else None)
        vendor = (w.get("Activity name") or "other").strip()
        ctype, is_res, _ = taxonomy.canonical_activity_type("whoop", vendor)

        # CSV gives zone 1..5 as a percentage of workout duration; the
        # below-zone-1 remainder is implicit.
        zones = {}
        acc = 0.0
        for i in range(1, 6):
            pct = num(w.get(f"HR Zone {i} %"))
            secs = (pct / 100.0 * dur_s) if (pct is not None and dur_s) else None
            zones[f"zone_{i}_s"] = secs
            acc += secs or 0
        zones["zone_0_s"] = max(dur_s - acc, 0) if dur_s else None

        kcal = num(w.get("Energy burned (cal)"))
        rows.append({
            "activity_uid": hash_uid("whoop_export", w.get("Workout start time"), vendor),
            "source": "whoop_export",
            "source_activity_id": f"{w.get('Workout start time')}|{vendor}",
            "start_ts": start,
            "end_ts": end,
            "day": local_day(start),
            "duration_s": dur_s,
            "moving_duration_s": None,
            "activity_type": ctype,
            "vendor_type": vendor,
            "name": vendor,
            "is_resistance": is_res,
            "strain": num(w.get("Activity Strain")),
            "training_load": None, "aerobic_te": None, "anaerobic_te": None,
            "rpe": None, "intensity_pct": None,
            "avg_hr": int(num(w.get("Average HR (bpm)")) or 0) or None,
            "max_hr": int(num(w.get("Max HR (bpm)")) or 0) or None,
            "min_hr": None,
            "kcal": kcal,
            "kilojoules": kcal * 4.184 if kcal else None,
            **zones,
            "distance_m": None, "elevation_gain_m": None, "steps": None,
            "total_sets": None, "total_reps": None, "total_volume_kg": None,
            "unique_exercises": None,
            "raw_id": r["id"],
            "payload": jsonb({"gps": w.get("GPS enabled")}),
            "updated_at": _now(),
        })
    n = upsert(conn, "fact_activity", rows, conflict=["activity_uid"])
    log.info("whoop.project.export_workout", rows=n)
    return n


def project_export_sleeps(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id, natural_key, occurred_on, payload FROM raw_import "
                    "WHERE source = 'whoop_export' AND entity = 'sleep'")
        raw = cur.fetchall()
    rows = []
    for r in raw:
        s = r["payload"]
        tz = parse_offset(s.get("Cycle timezone"))
        start = parse_ts(s.get("Sleep onset"), tz)
        end = parse_ts(s.get("Wake onset"), tz)
        if not start or not end:
            continue
        def m(key: str, row: dict = s) -> float | None:
            """Minutes column -> seconds."""
            v = num(row.get(key))
            return v * 60 if v is not None else None

        rows.append({
            "sleep_uid": hash_uid("whoop_export", s.get("Sleep onset"), s.get("Wake onset")),
            "source": "whoop_export",
            "source_sleep_id": f"{s.get('Sleep onset')}|{s.get('Wake onset')}",
            "start_ts": start,
            "end_ts": end,
            "day": local_day(end),
            "is_nap": str(s.get("Nap", "")).strip().lower() == "true",
            "time_in_bed_s": m("In bed duration (min)"),
            "asleep_s": m("Asleep duration (min)"),
            "awake_s": m("Awake duration (min)"),
            "light_s": m("Light sleep duration (min)"),
            "deep_s": m("Deep (SWS) duration (min)"),
            "rem_s": m("REM duration (min)"),
            "unmeasurable_s": None,
            "efficiency_pct": num(s.get("Sleep efficiency %")),
            "performance_pct": num(s.get("Sleep performance %")),
            "consistency_pct": num(s.get("Sleep consistency %")),
            "score": num(s.get("Sleep performance %")),
            "sleep_need_s": m("Sleep need (min)"),
            "sleep_debt_s": m("Sleep debt (min)"),
            "respiratory_rate": num(s.get("Respiratory rate (rpm)")),
            "avg_hr": None, "avg_spo2": None, "lowest_spo2": None,
            "avg_stress": None, "disturbance_count": None,
            "cycle_count": None, "restless_moments": None,
            "raw_id": r["id"], "payload": jsonb({}),
            "updated_at": _now(),
        })
    n = upsert(conn, "fact_sleep_session", rows, conflict=["sleep_uid"])
    log.info("whoop.project.export_sleep", rows=n)
    return n


_EXPORT_CYCLE_METRICS = {
    "Recovery score %":              ("recovery_score", "pct"),
    "Resting heart rate (bpm)":      ("resting_hr", "bpm"),
    "Heart rate variability (ms)":   ("hrv", "ms"),
    "Skin temp (celsius)":           ("skin_temp", "C"),
    "Blood oxygen %":                ("spo2_avg", "pct"),
    "Day Strain":                    ("day_strain", "score"),
    "Energy burned (cal)":           ("calories_total", "kcal"),
    "Max HR (bpm)":                  ("max_hr_day", "bpm"),
    "Average HR (bpm)":              ("avg_hr_day", "bpm"),
    "Respiratory rate (rpm)":        ("respiratory_rate", "rpm"),
    "Sleep performance %":           ("sleep_performance", "pct"),
    "Sleep efficiency %":            ("sleep_efficiency", "pct"),
    "Sleep consistency %":           ("sleep_consistency", "pct"),
    "Sleep need (min)":              ("sleep_need", "min"),
    "Sleep debt (min)":              ("sleep_debt", "min"),
    "Asleep duration (min)":         ("asleep_time", "min"),
    "In bed duration (min)":         ("time_in_bed", "min"),
}


def project_export_cycles(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT occurred_on, payload FROM raw_import "
                    "WHERE source = 'whoop_export' AND entity = 'cycle'")
        raw = cur.fetchall()
    rows = {}
    for r in raw:
        c = r["payload"]
        # A Whoop cycle starts in the evening and ends the next evening; the
        # day it describes is the WAKE day, which is the cycle's end date.
        tz = parse_offset(c.get("Cycle timezone"))
        wake = parse_ts(c.get("Wake onset"), tz)
        day = local_day(wake) if wake else r["occurred_on"]
        if not day:
            continue
        for col, (name, unit) in _EXPORT_CYCLE_METRICS.items():
            v = num(c.get(col))
            if v is None:
                continue
            rows[(day, name)] = {"day": day, "metric": name, "source": "whoop_export",
                                 "value": v, "unit": unit, "payload": jsonb({}),
                                 "updated_at": _now()}
    n = upsert(conn, "fact_daily_metric", list(rows.values()),
               conflict=["day", "metric", "source"])
    log.info("whoop.project.export_cycle", rows=n)
    return n


def project_export_journal(conn: psycopg.Connection) -> int:
    """Historical journal answers -> fact_habit_log, which already holds the
    live journal feed. Keyed on source_row_hash so the two never collide."""
    with conn.cursor() as cur:
        cur.execute("SELECT natural_key, occurred_on, payload FROM raw_import "
                    "WHERE source = 'whoop_export' AND entity = 'journal'")
        raw = cur.fetchall()
    # fact_habit_log carries a second unique index on (day, habit_key, source),
    # and two Whoop cycles can land on one calendar day. Collapse to the last
    # answer for the day so the upsert can't trip that index.
    by_day_key: dict[tuple, dict] = {}
    for r in raw:
        j = r["payload"]
        q = (j.get("Question text") or "").strip()
        if not q or not r["occurred_on"]:
            continue
        answered = str(j.get("Answered yes", "")).strip().lower()
        key = taxonomy.normalize_label(q)
        by_day_key[(r["occurred_on"], key)] = {
            "day": r["occurred_on"],
            "source": "whoop_export",
            "habit_key": key,
            "answered_yes": True if answered == "true" else (False if answered == "false" else None),
            "notes": (j.get("Notes") or None),
            "source_row_hash": r["natural_key"],
            "updated_at": _now(),
        }
    n = upsert(conn, "fact_habit_log", list(by_day_key.values()),
               conflict=["day", "habit_key", "source"],
               update=["answered_yes", "notes", "source_row_hash", "updated_at"])
    log.info("whoop.project.export_journal", rows=n)
    return n


def project_all(conn: psycopg.Connection) -> dict:
    out = {"public_workouts": project_public_workouts(conn)}
    out.update({f"lift_{k}": v for k, v in project_lifts(conn).items()})
    out["public_sleep"] = project_public_sleep(conn)
    out.update({f"trend_{k}": v for k, v in project_trends(conn).items()})
    out["export_workouts"] = project_export_workouts(conn)
    out["export_sleeps"] = project_export_sleeps(conn)
    out["export_cycles"] = project_export_cycles(conn)
    out["export_journal"] = project_export_journal(conn)
    return out
