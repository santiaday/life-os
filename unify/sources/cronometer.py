"""Cronometer mobile-API history -> canonical facts.

Two streams come out of `raw_import(source='cronometer')`:

  entity='diary'  one row per day: Serving entries (foodId + grams + time) and
                  Biometric entries (wearable passthrough from Apple Health,
                  Whoop and Garmin).
  entity='food'   the food catalogue, cached so macros can be resolved without
                  a request per serving. Nutrient amounts are per 100 g.

The biometric passthrough is the most valuable part: Cronometer has been
mirroring Apple Health weight since 2023-08-14, which is 16 months earlier
than any other source in the warehouse reaches.

`fact_food_log`-derived rows keep the source key `cronometer_csv` (see
unify.sources.nutrition) so the two Cronometer read paths never double-count
a day.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import psycopg

from ingest_cronometer.history import NUTRIENT_IDS
from lifeos_core.logging import get_logger
from unify.common import LOCAL_TZ, hash_uid, jsonb, lb_to_kg, num, uid, upsert

log = get_logger(__name__)

SOURCE = "cronometer"

# metricId -> (canonical metric, unit, transform). Confirmed by joining the
# diary payload against the already-named rows in fact_biometric.
BIOMETRIC_METRICS: dict[int, tuple[str, str]] = {
    1:     ("weight", "lb"),
    2:     ("height", "cm"),
    3:     ("heart_rate", "bpm"),
    8:     ("body_fat_pct", "pct"),
    764:   ("asleep_time", "hours"),
    32527: ("vo2_max", "ml/kg/min"),
    39062: ("spo2_avg", "pct"),
    40002: ("hrv", "ms"),
    47185: ("recovery_score", "pct"),
    47186: ("resting_hr", "bpm"),
    47187: ("skin_temp", "C"),
    50088: ("respiratory_rate", "rpm"),
    57418: ("sleep_performance", "pct"),
}

# Cronometer tags each biometric with the app it came from. Keep that, because
# an Apple Health weight and a Whoop HRV are different sources with different
# precedence even though they arrive through the same pipe.
VENDOR_SOURCE = {
    "Apple Health": "apple_health",
    "WHOOP": "whoop",
    "Garmin": "garmin",
    "": "cronometer",
}

_ENTRY_FIELDS = ("energy_kcal", "protein_g", "carbs_g", "fiber_g", "sugar_g",
                 "fat_g", "saturated_fat_g", "cholesterol_mg", "sodium_mg",
                 "potassium_mg", "caffeine_mg", "alcohol_g")


def _load_foods(conn: psycopg.Connection) -> dict[int, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT natural_key, payload FROM raw_import "
                    "WHERE source = %s AND entity = 'food'", [SOURCE])
        out = {}
        for r in cur.fetchall():
            f = r["payload"]
            nutrients = {}
            for n in f.get("nutrients") or []:
                name = NUTRIENT_IDS.get(n.get("id"))
                if name:
                    nutrients[name] = num(n.get("amount"))
            out[int(r["natural_key"])] = {"name": f.get("name") or f"food:{r['natural_key']}",
                                          "nutrients": nutrients}
        return out


def project_servings(conn: psycopg.Connection) -> dict[str, int]:
    foods = _load_foods(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT occurred_on, payload FROM raw_import "
                    "WHERE source = %s AND entity = 'diary' ORDER BY occurred_on",
                    [SOURCE])
        days = cur.fetchall()

    entries = []
    missing_foods: set[int] = set()
    for d in days:
        day = d["occurred_on"]
        for e in d["payload"].get("diary") or []:
            if e.get("type") != "Serving":
                continue
            fid = e.get("foodId")
            grams = num(e.get("grams"))
            food = foods.get(fid)
            if food is None:
                missing_foods.add(fid)
                continue
            factor = (grams or 0) / 100.0
            clock = e.get("time") or "12:00:00"
            try:
                t = time.fromisoformat(clock)
            except ValueError:
                t = time(12, 0)
            eaten_at = datetime.combine(day, t, tzinfo=LOCAL_TZ)
            nut = food["nutrients"]
            row = {
                "entry_uid": uid(SOURCE, e.get("servingId") or hash_uid("s", day, fid, clock)),
                "source": SOURCE,
                "source_entry_id": str(e.get("servingId") or ""),
                "eaten_at": eaten_at,
                "day": day,
                "meal": None,
                "food_name": food["name"],
                "brand": None,
                "amount": grams,
                "unit": "g",
                "micros": jsonb({k: round(v * factor, 4)
                                 for k, v in nut.items()
                                 if v is not None and k not in _ENTRY_FIELDS}),
                "raw_id": None,
                "payload": jsonb({"foodId": fid, "measureId": e.get("measureId")}),
                "updated_at": datetime.now(UTC),
            }
            for f in _ENTRY_FIELDS:
                v = nut.get(f)
                row[f] = round(v * factor, 4) if v is not None else None
            row["net_carbs_g"] = (
                round((row["carbs_g"] or 0) - (row["fiber_g"] or 0), 4)
                if row["carbs_g"] is not None else None)
            entries.append(row)

    if missing_foods:
        log.warning("cronometer.project.missing_foods", count=len(missing_foods))

    n_e = upsert(conn, "fact_nutrition_entry", entries, conflict=["entry_uid"])

    from unify.sources.nutrition import _rollup
    n_d = upsert(conn, "fact_nutrition_daily", _rollup(entries, SOURCE),
                 conflict=["day", "source"])
    log.info("cronometer.project.servings", entries=n_e, days=n_d,
             missing_foods=len(missing_foods))
    return {"entries": n_e, "days": n_d, "missing_foods": len(missing_foods)}


def project_biometrics(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT occurred_on, payload FROM raw_import "
                    "WHERE source = %s AND entity = 'diary' ORDER BY occurred_on",
                    [SOURCE])
        days = cur.fetchall()

    metric_rows: dict[tuple, dict] = {}
    weight_by: dict[tuple, dict] = {}
    now = datetime.now(UTC)

    for d in days:
        day = d["occurred_on"]
        for e in d["payload"].get("diary") or []:
            if e.get("type") != "Biometric":
                continue
            spec = BIOMETRIC_METRICS.get(e.get("metricId"))
            amount = num(e.get("amount"))
            if not spec or amount is None:
                continue
            metric, unit = spec
            vendor = VENDOR_SOURCE.get(e.get("source") or "", "cronometer")

            if metric in ("weight", "body_fat_pct"):
                slot = weight_by.setdefault((day, vendor), {})
                slot[metric] = amount
                continue

            metric_rows[(day, metric, vendor)] = {
                "day": day, "metric": metric, "source": vendor,
                "value": amount, "unit": unit,
                "payload": jsonb({"via": "cronometer", "externalId": e.get("externalId")}),
                "updated_at": now,
            }

    body_rows = []
    for (day, vendor), vals in weight_by.items():
        lb = vals.get("weight")
        # Values under 100 in a pounds-tagged field are a kilogram entry that
        # slipped through Cronometer's unit handling. Keep them as-is here and
        # let unify.quality flag the implausible ones -- silently "fixing" a
        # reading would destroy the evidence.
        kg = lb_to_kg(lb) if lb else None
        bf = vals.get("body_fat_pct")
        ts = datetime.combine(day, time(hour=7), tzinfo=LOCAL_TZ)
        body_rows.append({
            "measurement_uid": uid(vendor, "scale", int(ts.timestamp())),
            "source": vendor,
            "method": "bioimpedance" if bf is not None else "scale",
            "measured_at": ts,
            "day": day,
            "weight_kg": kg,
            "body_fat_pct": bf,
            "lean_mass_kg": (kg * (1 - bf / 100)) if (kg and bf) else None,
            "fat_mass_kg": (kg * bf / 100) if (kg and bf) else None,
            "bone_mineral_kg": None, "visceral_fat": None, "bmi": None,
            "muscle_mass_kg": None, "body_water_pct": None,
            "region": jsonb({}), "confidence": 0.5,
            "raw_id": None, "payload": jsonb({"weight_lb": lb, "via": "cronometer"}),
            "updated_at": now,
        })

    n_m = upsert(conn, "fact_daily_metric", list(metric_rows.values()),
                 conflict=["day", "metric", "source"])
    n_b = upsert(conn, "fact_body_composition", body_rows,
                 conflict=["measurement_uid"])
    log.info("cronometer.project.biometrics", metrics=n_m, body=n_b)
    return {"metrics": n_m, "body": n_b}


def project_all(conn: psycopg.Connection) -> dict:
    out = {f"serving_{k}": v for k, v in project_servings(conn).items()}
    out.update({f"biometric_{k}": v for k, v in project_biometrics(conn).items()})
    return out
