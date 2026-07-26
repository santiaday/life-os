"""raw_import(source='garmin') -> canonical facts.

Every unit conversion documented in ingest_files.garmin happens here, once,
at the adapter boundary. Downstream nothing knows Garmin exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from lifeos_core.logging import get_logger
from unify import taxonomy
from unify.common import (
    LOCAL_TZ,
    g_to_kg,
    jsonb,
    local_day,
    ms_to_s,
    num,
    resolve_and_register,
    uid,
    upsert,
)

log = get_logger(__name__)

SOURCE = "garmin"
CM_TO_M = 0.01


def _rows(conn: psycopg.Connection, entity: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, natural_key, occurred_on, payload FROM raw_import "
            "WHERE source = %s AND entity = %s ORDER BY occurred_on",
            [SOURCE, entity],
        )
        return cur.fetchall()


def _ts(epoch_ms) -> datetime | None:
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(float(epoch_ms) / 1000, UTC)


def _garmin_local_ts(iso: str | None) -> datetime | None:
    """Garmin wellness timestamps are naive ISO strings already in local time
    for `*Local` fields and in UTC for `*GMT` fields."""
    if not iso:
        return None
    return datetime.fromisoformat(iso[:19])


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

def project_activities(conn: psycopg.Connection) -> dict[str, int]:
    raw = _rows(conn, "activity")
    acts, exercises = [], []
    for r in raw:
        a = r["payload"]
        start = _ts(a.get("startTimeGmt") or a.get("beginTimestamp"))
        if start is None:
            continue
        dur_s = ms_to_s(a.get("duration"))
        end = start + timedelta(seconds=dur_s) if dur_s else None
        vendor_type = a.get("activityType") or ""
        ctype, is_res, _ = taxonomy.canonical_activity_type(SOURCE, vendor_type)
        auid = uid(SOURCE, a["activityId"])

        # hrTimeInZone_0..6: index 0 is below zone 1; Garmin's zone 5 is
        # index 5. Index 6 exists in the schema but is always 0 here.
        zones = [ms_to_s(a.get(f"hrTimeInZone_{i}")) for i in range(7)]

        sets = a.get("summarizedExerciseSets") or []
        total_vol_kg = sum(g_to_kg(s.get("volume")) or 0 for s in sets) or None

        acts.append({
            "activity_uid": auid,
            "source": SOURCE,
            "source_activity_id": str(a["activityId"]),
            "start_ts": start,
            "end_ts": end,
            "day": local_day(start),
            "duration_s": dur_s,
            "moving_duration_s": ms_to_s(a.get("movingDuration")),
            "activity_type": ctype,
            "vendor_type": vendor_type,
            "name": a.get("name"),
            "is_resistance": is_res,
            "strain": None,
            "training_load": num(a.get("activityTrainingLoad")),
            "aerobic_te": num(a.get("aerobicTrainingEffect")),
            "anaerobic_te": num(a.get("anaerobicTrainingEffect")),
            "rpe": num(a.get("workoutRpe")),
            "intensity_pct": None,
            "avg_hr": int(a["avgHr"]) if num(a.get("avgHr")) else None,
            "max_hr": int(a["maxHr"]) if num(a.get("maxHr")) else None,
            "min_hr": int(a["minHr"]) if num(a.get("minHr")) else None,
            "kcal": num(a.get("calories")),
            "kilojoules": (num(a.get("calories")) * 4.184) if num(a.get("calories")) else None,
            "zone_0_s": zones[0], "zone_1_s": zones[1], "zone_2_s": zones[2],
            "zone_3_s": zones[3], "zone_4_s": zones[4], "zone_5_s": zones[5],
            "distance_m": (num(a.get("distance")) or 0) * CM_TO_M or None,
            "elevation_gain_m": ((num(a.get("elevationGain")) or 0) * CM_TO_M) or None,
            "steps": int(a["steps"]) if num(a.get("steps")) else None,
            "total_sets": a.get("totalSets"),
            "total_reps": a.get("totalReps"),
            "total_volume_kg": total_vol_kg,
            "unique_exercises": len(sets) or None,
            "raw_id": r["id"],
            "payload": jsonb({
                "workoutId": a.get("workoutId"),
                "deviceId": a.get("deviceId"),
                "trainingEffectLabel": a.get("trainingEffectLabel"),
                "moderateIntensityMinutes": a.get("moderateIntensityMinutes"),
                "vigorousIntensityMinutes": a.get("vigorousIntensityMinutes"),
                "vO2MaxValue": a.get("vO2MaxValue"),
                "avgPower": a.get("avgPower"),
                "maxPower": a.get("maxPower"),
                "workoutFeel": a.get("workoutFeel"),
                "locationName": a.get("locationName"),
                "activeSets": a.get("activeSets"),
                "waterEstimatedMl": a.get("waterEstimated"),
            }),
            "updated_at": datetime.now(UTC),
        })

        for idx, s in enumerate(sets):
            label = s.get("subCategory") or s.get("category") or "UNKNOWN"
            key = resolve_and_register(conn, SOURCE, label,
                                       vendor_id=label,
                                       vendor_category=s.get("category"))
            exercises.append({
                "activity_uid": auid,
                "exercise_index": idx,
                "exercise_key": key,
                "vendor_exercise": label,
                "vendor_category": s.get("category"),
                "source": SOURCE,
                "day": local_day(start),
                # Garmin only ever exports per-exercise aggregates -- there is
                # no per-set breakdown in the GDPR export.
                "granularity": "exercise",
                "set_count": s.get("sets"),
                "total_reps": s.get("reps"),
                "total_volume_kg": g_to_kg(s.get("volume")),
                "max_weight_kg": g_to_kg(s.get("maxWeight")),
                "duration_s": ms_to_s(s.get("duration")),
                "is_pr": False,
                "updated_at": datetime.now(UTC),
            })

    n_a = upsert(conn, "fact_activity", acts, conflict=["activity_uid"])
    n_e = upsert(conn, "fact_activity_exercise", exercises,
                 conflict=["activity_uid", "exercise_index"])
    log.info("garmin.project.activity", activities=n_a, exercises=n_e)
    return {"activities": n_a, "exercises": n_e}


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

def project_sleep(conn: psycopg.Connection) -> int:
    raw = _rows(conn, "sleep")
    rows = []
    for r in raw:
        s = r["payload"]
        start = _garmin_local_ts(s.get("sleepStartTimestampGMT"))
        end = _garmin_local_ts(s.get("sleepEndTimestampGMT"))
        if not start or not end:
            continue
        start = start.replace(tzinfo=UTC)
        end = end.replace(tzinfo=UTC)
        deep = num(s.get("deepSleepSeconds")) or 0
        light = num(s.get("lightSleepSeconds")) or 0
        rem = num(s.get("remSleepSeconds")) or 0
        awake = num(s.get("awakeSleepSeconds")) or 0
        unmeasurable = num(s.get("unmeasurableSeconds")) or 0
        asleep = deep + light + rem
        in_bed = asleep + awake + unmeasurable
        scores = s.get("sleepScores") or {}
        spo2 = s.get("spo2SleepSummary") or {}

        rows.append({
            "sleep_uid": uid(SOURCE, s["calendarDate"]),
            "source": SOURCE,
            "source_sleep_id": s["calendarDate"],
            "start_ts": start,
            "end_ts": end,
            "day": local_day(end),
            "is_nap": False,
            "time_in_bed_s": in_bed or None,
            "asleep_s": asleep or None,
            "awake_s": awake,
            "light_s": light,
            "deep_s": deep,
            "rem_s": rem,
            "unmeasurable_s": unmeasurable,
            "efficiency_pct": round(asleep / in_bed * 100, 1) if in_bed else None,
            "performance_pct": None,
            "consistency_pct": None,
            "score": num(scores.get("overallScore")),
            "respiratory_rate": num(s.get("averageRespiration")),
            "avg_hr": num(spo2.get("averageHR")),
            "avg_spo2": num(spo2.get("averageSPO2")),
            "lowest_spo2": num(spo2.get("lowestSPO2")),
            "avg_stress": num(s.get("avgSleepStress")),
            "disturbance_count": s.get("awakeCount"),
            "restless_moments": s.get("restlessMomentCount"),
            "raw_id": r["id"],
            "payload": jsonb({"sleepScores": scores,
                              "breathingDisruptionSeverity": s.get("breathingDisruptionSeverity"),
                              "sleepWindowConfirmationType": s.get("sleepWindowConfirmationType")}),
            "updated_at": datetime.now(UTC),
        })

        for i, nap in enumerate(s.get("napList") or []):
            n_start = _garmin_local_ts(nap.get("napStartTimestampGMT"))
            n_end = _garmin_local_ts(nap.get("napEndTimestampGMT"))
            if not n_start or not n_end:
                continue
            n_start, n_end = n_start.replace(tzinfo=UTC), n_end.replace(tzinfo=UTC)
            rows.append({
                "sleep_uid": uid(SOURCE, f"{s['calendarDate']}:nap{i}"),
                "source": SOURCE,
                "source_sleep_id": f"{s['calendarDate']}:nap{i}",
                "start_ts": n_start,
                "end_ts": n_end,
                "day": local_day(n_end),
                "is_nap": True,
                "time_in_bed_s": num(nap.get("napTimeSec")),
                "asleep_s": num(nap.get("napTimeSec")),
                "awake_s": None, "light_s": None, "deep_s": None, "rem_s": None,
                "unmeasurable_s": None, "efficiency_pct": None,
                "performance_pct": None, "consistency_pct": None, "score": None,
                "respiratory_rate": None, "avg_hr": None, "avg_spo2": None,
                "lowest_spo2": None, "avg_stress": None,
                "disturbance_count": None, "restless_moments": None,
                "raw_id": r["id"], "payload": jsonb(nap),
                "updated_at": datetime.now(UTC),
            })

    n = upsert(conn, "fact_sleep_session", rows, conflict=["sleep_uid"])
    log.info("garmin.project.sleep", rows=n)
    return n


# ---------------------------------------------------------------------------
# Daily wellness metrics
# ---------------------------------------------------------------------------

# UDS field -> (canonical metric name, unit, transform)
_UDS_METRICS: dict[str, tuple[str, str, callable]] = {
    "totalSteps":               ("steps", "count", lambda v: v),
    "totalKilocalories":        ("calories_total", "kcal", lambda v: v),
    "activeKilocalories":       ("calories_active", "kcal", lambda v: v),
    "bmrKilocalories":          ("calories_bmr", "kcal", lambda v: v),
    "totalDistanceMeters":      ("distance", "m", lambda v: v),
    "restingHeartRate":         ("resting_hr", "bpm", lambda v: v),
    "minHeartRate":             ("min_hr", "bpm", lambda v: v),
    "maxHeartRate":             ("max_hr", "bpm", lambda v: v),
    "moderateIntensityMinutes": ("intensity_minutes_moderate", "min", lambda v: v),
    "vigorousIntensityMinutes": ("intensity_minutes_vigorous", "min", lambda v: v),
    "highlyActiveSeconds":      ("highly_active_time", "s", lambda v: v),
    "activeSeconds":            ("active_time", "s", lambda v: v),
    "floorsAscendedInMeters":   ("floors_ascended", "m", lambda v: v),
    "averageSpo2Value":         ("spo2_avg", "pct", lambda v: v),
    "lowestSpo2Value":          ("spo2_min", "pct", lambda v: v),
}


def project_daily(conn: psycopg.Connection) -> int:
    raw = _rows(conn, "daily")
    rows = []
    now = datetime.now(UTC)
    for r in raw:
        d = r["payload"]
        day = d.get("calendarDate")
        if not day:
            continue
        for field, (metric, unit, fn) in _UDS_METRICS.items():
            v = num(d.get(field))
            if v is None:
                continue
            rows.append({"day": day, "metric": metric, "source": SOURCE,
                         "value": fn(v), "unit": unit, "payload": jsonb({}),
                         "updated_at": now})

        stress = (d.get("allDayStress") or {}).get("aggregatorList") or []
        for agg in stress:
            if agg.get("type") == "TOTAL" and num(agg.get("averageStressLevel")) is not None:
                rows.append({"day": day, "metric": "stress_avg", "source": SOURCE,
                             "value": num(agg["averageStressLevel"]), "unit": "score",
                             "payload": jsonb({"max": agg.get("maxStressLevel")}),
                             "updated_at": now})

        bb = d.get("bodyBattery")
        if isinstance(bb, dict):
            for field, metric in (("charged", "body_battery_charged"),
                                  ("drained", "body_battery_drained")):
                v = num(bb.get(field))
                if v is not None:
                    rows.append({"day": day, "metric": metric, "source": SOURCE,
                                 "value": v, "unit": "score", "payload": jsonb({}),
                                 "updated_at": now})

        resp = d.get("respiration")
        if isinstance(resp, dict) and num(resp.get("avgWakingRespirationValue")) is not None:
            rows.append({"day": day, "metric": "respiratory_rate", "source": SOURCE,
                         "value": num(resp["avgWakingRespirationValue"]), "unit": "rpm",
                         "payload": jsonb({}), "updated_at": now})

    # VO2 max comes from its own metrics file, not UDS.
    for r in _rows(conn, "vo2max"):
        v = r["payload"]
        if not r["occurred_on"] or num(v.get("vo2MaxValue")) is None:
            continue
        rows.append({"day": r["occurred_on"], "metric": "vo2_max", "source": SOURCE,
                     "value": num(v["vo2MaxValue"]), "unit": "ml/kg/min",
                     "payload": jsonb({"sport": v.get("sport")}), "updated_at": now})

    for r in _rows(conn, "fitness_age"):
        v = r["payload"]
        if not r["occurred_on"]:
            continue
        if num(v.get("currentBioAge")) is not None:
            rows.append({"day": r["occurred_on"], "metric": "fitness_age", "source": SOURCE,
                         "value": num(v["currentBioAge"]), "unit": "years",
                         "payload": jsonb({}), "updated_at": now})

    for r in _rows(conn, "hydration"):
        v = r["payload"]
        if not r["occurred_on"]:
            continue
        sweat = num(v.get("estimatedSweatLossInML"))
        if sweat:
            rows.append({"day": r["occurred_on"], "metric": "sweat_loss", "source": SOURCE,
                         "value": sweat, "unit": "ml", "payload": jsonb({}),
                         "updated_at": now})

    # Collapse duplicates on (day, metric) -- hydration/UDS can both emit.
    dedup: dict[tuple, dict] = {}
    for row in rows:
        k = (row["day"], row["metric"], row["source"])
        if k in dedup:
            dedup[k]["value"] = (dedup[k]["value"] or 0) + (row["value"] or 0)
        else:
            dedup[k] = row
    out = list(dedup.values())

    n = upsert(conn, "fact_daily_metric", out, conflict=["day", "metric", "source"])
    log.info("garmin.project.daily_metric", rows=n)
    return n


# ---------------------------------------------------------------------------
# Body composition
# ---------------------------------------------------------------------------

def project_body(conn: psycopg.Connection) -> int:
    raw = _rows(conn, "biometrics")
    rows = []
    now = datetime.now(UTC)
    for r in raw:
        b = r["payload"]
        w = b.get("weight") or {}
        if not w:
            continue
        ts_raw = w.get("timestampGMT") or (b.get("metaData") or {}).get("calendarDate")
        ts = _garmin_local_ts(ts_raw)
        if ts is None:
            continue
        ts = ts.replace(tzinfo=LOCAL_TZ)
        weight_kg = g_to_kg(w.get("weight"))
        bf = num(w.get("bodyFat"))
        # Garmin's sourceType tells us whether this came off a smart scale or
        # was typed in. That distinction is the whole point of GAP-9.
        src_type = (w.get("sourceType") or "").upper()
        method = {"MANUAL": "manual", "INDEX_SCALE": "bioimpedance",
                  "SCALE": "scale", "CHEST_STRAP": "estimate"}.get(src_type, "manual")
        rows.append({
            "measurement_uid": uid(SOURCE, method, int(ts.timestamp())),
            "source": SOURCE,
            "method": method,
            "measured_at": ts,
            "day": local_day(ts),
            "weight_kg": weight_kg,
            "body_fat_pct": bf,
            "lean_mass_kg": (weight_kg * (1 - bf / 100)) if (weight_kg and bf) else None,
            "fat_mass_kg": (weight_kg * bf / 100) if (weight_kg and bf) else None,
            "bone_mineral_kg": None,
            "visceral_fat": None,
            "bmi": num(w.get("bmi")),
            "muscle_mass_kg": g_to_kg(w.get("muscleMass")),
            "body_water_pct": num(w.get("bodyWater")),
            "region": jsonb({}),
            "confidence": 0.5 if method == "manual" else 0.4,
            "raw_id": r["id"],
            "payload": jsonb(w),
            "updated_at": now,
        })
    n = upsert(conn, "fact_body_composition", rows, conflict=["measurement_uid"])
    log.info("garmin.project.body", rows=n)
    return n


def project_personal_records(conn: psycopg.Connection) -> int:
    """Flag exercises that carry a Garmin PR so vw_activity_exercise.is_pr is
    populated for the Garmin window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE fact_activity_exercise e
               SET is_pr = TRUE
              FROM raw_import r
             WHERE r.source = 'garmin' AND r.entity = 'personal_record'
               AND (r.payload->>'activityId') <> '0'
               AND e.activity_uid = 'garmin:' || (r.payload->>'activityId')
            """
        )
        return cur.rowcount


def project_all(conn: psycopg.Connection) -> dict:
    out = {}
    out.update(project_activities(conn))
    out["sleep"] = project_sleep(conn)
    out["daily_metrics"] = project_daily(conn)
    out["body"] = project_body(conn)
    out["prs"] = project_personal_records(conn)
    return out
