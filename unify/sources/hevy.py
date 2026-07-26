"""Hevy -> canonical facts.

Hevy is retired as a live source (strength moved to Whoop's Strength Trainer)
but it holds true per-set data for May-June 2026, so it stays in the unified
layer as history.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from lifeos_core.logging import get_logger
from unify.common import jsonb, local_day, resolve_and_register, uid, upsert

log = get_logger(__name__)

SOURCE = "hevy"


def project_all(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_strength_workout")
        workouts = cur.fetchall()
        cur.execute("SELECT * FROM fact_strength_set "
                    "ORDER BY hevy_workout_id, exercise_index, set_index")
        sets = cur.fetchall()

    by_workout: dict[str, list[dict]] = {}
    for s in sets:
        by_workout.setdefault(str(s["hevy_workout_id"]), []).append(s)

    acts, ex_rows, set_rows = [], [], []
    now = datetime.now(UTC)

    for w in workouts:
        wid = str(w["hevy_workout_id"])
        auid = uid(SOURCE, wid)
        mysets = by_workout.get(wid, [])
        acts.append({
            "activity_uid": auid,
            "source": SOURCE,
            "source_activity_id": wid,
            "start_ts": w["start_ts"],
            "end_ts": w["end_ts"],
            "day": w["day"] or local_day(w["start_ts"]),
            "duration_s": w["duration_seconds"],
            "moving_duration_s": None,
            "activity_type": "strength",
            "vendor_type": "strength",
            "name": w["title"],
            "is_resistance": True,
            "strain": None, "training_load": None, "aerobic_te": None,
            "anaerobic_te": None, "rpe": None, "intensity_pct": None,
            "avg_hr": None, "max_hr": None, "min_hr": None,
            "kcal": None, "kilojoules": None,
            "zone_0_s": None, "zone_1_s": None, "zone_2_s": None,
            "zone_3_s": None, "zone_4_s": None, "zone_5_s": None,
            "distance_m": None, "elevation_gain_m": None, "steps": None,
            "total_sets": w["total_sets"],
            "total_reps": w["total_reps"],
            "total_volume_kg": w["total_volume_kg"],
            "unique_exercises": w["unique_exercises"],
            "raw_id": None,
            "payload": jsonb({"description": w["description"]}),
            "updated_at": now,
        })

        grouped: dict[int, list[dict]] = {}
        for s in mysets:
            grouped.setdefault(s["exercise_index"], []).append(s)

        for ei, group in sorted(grouped.items()):
            label = group[0]["exercise_title"]
            key = resolve_and_register(conn, SOURCE, label,
                                       vendor_id=group[0]["exercise_template_id"])
            weights = [float(s["weight_kg"]) for s in group if s["weight_kg"]]
            ex_rows.append({
                "activity_uid": auid, "exercise_index": ei,
                "exercise_key": key, "vendor_exercise": label,
                "vendor_category": None, "source": SOURCE,
                "day": w["day"] or local_day(w["start_ts"]),
                "granularity": "set",
                "set_count": len(group),
                "total_reps": sum(s["reps"] or 0 for s in group) or None,
                "total_volume_kg": sum((s["reps"] or 0) * float(s["weight_kg"] or 0)
                                       for s in group) or None,
                "max_weight_kg": max(weights) if weights else None,
                "duration_s": sum(s["duration_seconds"] or 0 for s in group) or None,
                "is_pr": False, "updated_at": now,
            })
            for s in group:
                set_rows.append({
                    "activity_uid": auid,
                    "exercise_index": ei,
                    "set_index": s["set_index"],
                    "exercise_key": key,
                    "vendor_exercise": label,
                    "source": SOURCE,
                    "day": s["day"] or local_day(s["workout_start_ts"]),
                    "set_type": s["set_type"],
                    "reps": s["reps"],
                    "weight_kg": s["weight_kg"],
                    "duration_s": s["duration_seconds"],
                    "distance_m": s["distance_meters"],
                    "rpe": s["rpe"],
                    "avg_hr": None,
                    "is_pr": False,
                    "volume_kg": (s["reps"] or 0) * float(s["weight_kg"] or 0) or None,
                    "updated_at": now,
                })

    n_a = upsert(conn, "fact_activity", acts, conflict=["activity_uid"])
    n_e = upsert(conn, "fact_activity_exercise", ex_rows,
                 conflict=["activity_uid", "exercise_index"])
    n_s = upsert(conn, "fact_activity_set", set_rows,
                 conflict=["activity_uid", "exercise_index", "set_index"])
    log.info("hevy.project", activities=n_a, exercises=n_e, sets=n_s)
    return {"activities": n_a, "exercises": n_e, "sets": n_s}
