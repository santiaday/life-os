"""Lose It! -> canonical facts.

Lose It has no per-item timestamps, only a meal name. Entries are placed at a
representative hour per meal so ordering within a day is preserved and
eating-window maths stays meaningful; the exact minute is fiction and callers
should treat it as such.

Weights are pounds. Sleep is decimal hours. Steps and the nutrient trend files
are daily scalars.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import psycopg

from lifeos_core.logging import get_logger
from unify import taxonomy
from unify.common import (
    LOCAL_TZ,
    hash_uid,
    jsonb,
    lb_to_kg,
    num,
    uid,
    upsert,
)

log = get_logger(__name__)

SOURCE = "loseit"

# Representative local clock time per meal, used because the export has none.
MEAL_HOUR = {
    "breakfast": 8, "lunch": 12, "dinner": 19,
    "snacks": 15, "snack": 15, "other": 15,
}


def _rows(conn: psycopg.Connection, entity: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, natural_key, occurred_on, payload FROM raw_import "
            "WHERE source = %s AND entity = %s ORDER BY occurred_on",
            [SOURCE, entity],
        )
        return cur.fetchall()


def _deleted(payload: dict) -> bool:
    v = str(payload.get("Deleted", "")).strip().lower()
    return v in ("1", "true", "yes")


def project_food(conn: psycopg.Connection) -> dict[str, int]:
    entries = []
    for r in _rows(conn, "food_log"):
        f = r["payload"]
        day = r["occurred_on"]
        if not day or _deleted(f):
            continue
        meal = (f.get("Meal") or "other").strip().lower()
        hour = MEAL_HOUR.get(meal, 15)
        eaten_at = datetime.combine(day, time(hour=hour), tzinfo=LOCAL_TZ)
        entries.append({
            "entry_uid": hash_uid(SOURCE, r["natural_key"]),
            "source": SOURCE,
            "source_entry_id": r["natural_key"],
            "eaten_at": eaten_at,
            "day": day,
            "meal": "snack" if meal.startswith("snack") else meal,
            "food_name": (f.get("Name") or "").strip() or "Unnamed",
            "brand": None,
            "amount": num(f.get("Quantity")),
            "unit": f.get("Units"),
            "energy_kcal": num(f.get("Calories")),
            "protein_g": num(f.get("Protein (g)")),
            "carbs_g": num(f.get("Carbohydrates (g)")),
            "net_carbs_g": None,
            "fiber_g": num(f.get("Fiber (g)")),
            "sugar_g": num(f.get("Sugars (g)")),
            "fat_g": num(f.get("Fat (g)")),
            "saturated_fat_g": num(f.get("Saturated Fat (g)")),
            "cholesterol_mg": num(f.get("Cholesterol (mg)")),
            "sodium_mg": num(f.get("Sodium (mg)")),
            "potassium_mg": None, "caffeine_mg": None, "alcohol_g": None,
            "micros": jsonb({}),
            "raw_id": r["id"],
            "payload": jsonb({"icon": f.get("Icon")}),
            "updated_at": datetime.now(UTC),
        })
    n_e = upsert(conn, "fact_nutrition_entry", entries, conflict=["entry_uid"])

    # Daily totals: prefer the rollup of surviving entries, and fall back to
    # the vendor's own daily summary on days where only that exists (the
    # nutrient trend files cover days the item-level log does not).
    daily: dict = {}
    for e in entries:
        d = daily.setdefault(e["day"], {
            "energy_kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fiber_g": 0.0,
            "sugar_g": 0.0, "fat_g": 0.0, "saturated_fat_g": 0.0,
            "cholesterol_mg": 0.0, "sodium_mg": 0.0, "entry_count": 0,
            "first": e["eaten_at"], "last": e["eaten_at"],
        })
        for k in ("energy_kcal", "protein_g", "carbs_g", "fiber_g", "sugar_g",
                  "fat_g", "saturated_fat_g", "cholesterol_mg", "sodium_mg"):
            d[k] += e[k] or 0
        d["entry_count"] += 1
        d["first"] = min(d["first"], e["eaten_at"])
        d["last"] = max(d["last"], e["eaten_at"])

    # Vendor-reported daily scalars for days with no item-level rows.
    scalar_files = {"protein": "protein_g", "fat": "fat_g",
                    "fiber": "fiber_g", "sugar": "sugar_g"}
    summary: dict = {}
    for r in _rows(conn, "daily_summary"):
        kcal = num(r["payload"].get("Food cals"))
        if r["occurred_on"] and kcal:
            summary.setdefault(r["occurred_on"], {})["energy_kcal"] = kcal
    for entity, col in scalar_files.items():
        for r in _rows(conn, entity):
            v = num(r["payload"].get("Value"))
            if r["occurred_on"] and v is not None and v > 0:
                summary.setdefault(r["occurred_on"], {})[col] = v

    rows = []
    now = datetime.now(UTC)
    for day in set(daily) | set(summary):
        d = daily.get(day)
        s = summary.get(day, {})
        if d:
            rows.append({
                "day": day, "source": SOURCE,
                "energy_kcal": round(d["energy_kcal"], 1) or s.get("energy_kcal"),
                "protein_g": round(d["protein_g"], 1),
                "carbs_g": round(d["carbs_g"], 1),
                "net_carbs_g": None,
                "fiber_g": round(d["fiber_g"], 1),
                "sugar_g": round(d["sugar_g"], 1),
                "fat_g": round(d["fat_g"], 1),
                "saturated_fat_g": round(d["saturated_fat_g"], 1),
                "cholesterol_mg": round(d["cholesterol_mg"], 1),
                "sodium_mg": round(d["sodium_mg"], 1),
                "potassium_mg": None, "caffeine_mg": None, "alcohol_g": None,
                "entry_count": d["entry_count"],
                "first_eaten_at": d["first"], "last_eaten_at": d["last"],
                "is_rollup": True, "micros": jsonb({}), "updated_at": now,
            })
        else:
            rows.append({
                "day": day, "source": SOURCE,
                "energy_kcal": s.get("energy_kcal"),
                "protein_g": s.get("protein_g"),
                "carbs_g": None, "net_carbs_g": None,
                "fiber_g": s.get("fiber_g"),
                "sugar_g": s.get("sugar_g"),
                "fat_g": s.get("fat_g"),
                "saturated_fat_g": None, "cholesterol_mg": None,
                "sodium_mg": None, "potassium_mg": None,
                "caffeine_mg": None, "alcohol_g": None,
                "entry_count": 0, "first_eaten_at": None, "last_eaten_at": None,
                "is_rollup": False, "micros": jsonb({}), "updated_at": now,
            })
    n_d = upsert(conn, "fact_nutrition_daily", rows, conflict=["day", "source"])
    log.info("loseit.project.food", entries=n_e, days=n_d)
    return {"entries": n_e, "days": n_d}


def project_weight(conn: psycopg.Connection) -> int:
    rows = []
    for r in _rows(conn, "weight"):
        w = r["payload"]
        lb = num(w.get("Weight"))
        if not r["occurred_on"] or not lb or _deleted(w):
            continue
        ts = datetime.combine(r["occurred_on"], time(hour=7), tzinfo=LOCAL_TZ)
        rows.append({
            "measurement_uid": uid(SOURCE, "scale", int(ts.timestamp())),
            "source": SOURCE,
            "method": "scale",
            "measured_at": ts,
            "day": r["occurred_on"],
            "weight_kg": lb_to_kg(lb),
            "body_fat_pct": None, "lean_mass_kg": None, "fat_mass_kg": None,
            "bone_mineral_kg": None, "visceral_fat": None, "bmi": None,
            "muscle_mass_kg": None, "body_water_pct": None,
            "region": jsonb({}), "confidence": 0.6,
            "raw_id": r["id"], "payload": jsonb({"weight_lb": lb}),
            "updated_at": datetime.now(UTC),
        })
    n = upsert(conn, "fact_body_composition", rows, conflict=["measurement_uid"])
    log.info("loseit.project.weight", rows=n)
    return n


def project_exercise(conn: psycopg.Connection) -> int:
    """Self-reported exercise rows. 'Calorie Burn Bonus' is Lose It's internal
    budget adjustment, not a session -- dropped."""
    rows = []
    for r in _rows(conn, "exercise_log"):
        e = r["payload"]
        name = (e.get("Name") or "").strip()
        if not r["occurred_on"] or _deleted(e) or name == "Calorie Burn Bonus":
            continue
        qty = num(e.get("Quantity")) or 0
        units = (e.get("Units") or "").lower()
        dur_s = qty * 60 if "min" in units else None
        if not dur_s:
            continue
        start = datetime.combine(r["occurred_on"], time(hour=17), tzinfo=LOCAL_TZ)
        ctype, is_res, _ = taxonomy.canonical_activity_type(SOURCE, name)
        rows.append({
            "activity_uid": hash_uid(SOURCE, r["natural_key"]),
            "source": SOURCE,
            "source_activity_id": r["natural_key"],
            "start_ts": start,
            "end_ts": None,
            "day": r["occurred_on"],
            "duration_s": dur_s,
            "moving_duration_s": None,
            "activity_type": ctype,
            "vendor_type": name,
            "name": name,
            "is_resistance": is_res,
            "strain": None, "training_load": None, "aerobic_te": None,
            "anaerobic_te": None, "rpe": None, "intensity_pct": None,
            "avg_hr": None, "max_hr": None, "min_hr": None,
            "kcal": abs(num(e.get("Calories")) or 0) or None,
            "kilojoules": None,
            "zone_0_s": None, "zone_1_s": None, "zone_2_s": None,
            "zone_3_s": None, "zone_4_s": None, "zone_5_s": None,
            "distance_m": None, "elevation_gain_m": None, "steps": None,
            "total_sets": None, "total_reps": None, "total_volume_kg": None,
            "unique_exercises": None,
            "raw_id": r["id"], "payload": jsonb({"icon": e.get("Icon")}),
            "updated_at": datetime.now(UTC),
        })
    n = upsert(conn, "fact_activity", rows, conflict=["activity_uid"])
    log.info("loseit.project.exercise", rows=n)
    return n


def project_sleep(conn: psycopg.Connection) -> int:
    """Manually logged hours. No start time, so anchor at 23:00 the night
    before and let precedence keep it out of the way of real wearable data."""
    rows = []
    for r in _rows(conn, "sleep"):
        hours = num(r["payload"].get("Value"))
        if not r["occurred_on"] or not hours or hours <= 0:
            continue
        end = datetime.combine(r["occurred_on"], time(hour=7), tzinfo=LOCAL_TZ)
        start = end - timedelta(hours=hours)
        rows.append({
            "sleep_uid": uid(SOURCE, r["occurred_on"].isoformat()),
            "source": SOURCE,
            "source_sleep_id": r["occurred_on"].isoformat(),
            "start_ts": start, "end_ts": end, "day": r["occurred_on"],
            "is_nap": False,
            "time_in_bed_s": hours * 3600, "asleep_s": hours * 3600,
            "awake_s": None, "light_s": None, "deep_s": None, "rem_s": None,
            "unmeasurable_s": None, "efficiency_pct": None,
            "performance_pct": None, "consistency_pct": None, "score": None,
            "sleep_need_s": None, "sleep_debt_s": None,
            "respiratory_rate": None, "avg_hr": None, "avg_spo2": None,
            "lowest_spo2": None, "avg_stress": None,
            "disturbance_count": None, "cycle_count": None,
            "restless_moments": None,
            "raw_id": r["id"], "payload": jsonb({}),
            "updated_at": datetime.now(UTC),
        })
    n = upsert(conn, "fact_sleep_session", rows, conflict=["sleep_uid"])
    log.info("loseit.project.sleep", rows=n)
    return n


_SCALAR_METRICS = {
    "steps":            ("steps", "count"),
    "exercise_minutes": ("exercise_minutes", "min"),
    "garmin_calories":  ("calories_total", "kcal"),
    "water":            ("water_intake", "oz"),
}


def project_metrics(conn: psycopg.Connection) -> int:
    rows = []
    now = datetime.now(UTC)
    for entity, (metric, unit) in _SCALAR_METRICS.items():
        for r in _rows(conn, entity):
            v = num(r["payload"].get("Value"))
            if not r["occurred_on"] or v is None or v <= 0:
                continue
            rows.append({"day": r["occurred_on"], "metric": metric, "source": SOURCE,
                         "value": v, "unit": unit, "payload": jsonb({}),
                         "updated_at": now})
    dedup = {(r["day"], r["metric"], r["source"]): r for r in rows}
    n = upsert(conn, "fact_daily_metric", list(dedup.values()),
               conflict=["day", "metric", "source"])
    log.info("loseit.project.metrics", rows=n)
    return n


def project_all(conn: psycopg.Connection) -> dict:
    out = {f"food_{k}": v for k, v in project_food(conn).items()}
    out["weight"] = project_weight(conn)
    out["exercise"] = project_exercise(conn)
    out["sleep"] = project_sleep(conn)
    out["metrics"] = project_metrics(conn)
    return out
