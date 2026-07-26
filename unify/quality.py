"""Data-quality flagging -- GAP-8.

Two artifacts materially corrupted the original audit:

  * a 239.4 lb weight reading on 2024-09-30 sitting among eight same-day
    readings in the 176-179 lb range, which manufactured a fake -18 lb/4-week
    window that read as the best result in three years;
  * a 25.85% body-fat reading in Sep 2024 implying -4.94 kg fat *and* +1.87 kg
    lean in 30 days, which is physiologically impossible and produced a
    "recomp" conclusion that had to be retracted.

Neither is deleted. Both are flagged. A 239.4 lb reading is informative -- it
says something about the scale or the logging path -- and deleting it destroys
that evidence. Read paths filter on `data_quality_flag` instead, and default
to excluding flagged rows.

Three rule families:
  plausibility     absolute bounds a value cannot physically leave
  dispersion       a reading far from the same day's median across sources
  rate_of_change   a day-over-day delta biology does not permit
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

import psycopg

from lifeos_core.logging import get_logger
from unify.common import upsert

log = get_logger(__name__)

# metric -> (low, high, unit) absolute bounds. Outside these, the reading is
# not a measurement of this person.
PLAUSIBILITY: dict[str, tuple[float, float]] = {
    "weight_kg": (40.0, 200.0),
    "body_fat_pct": (3.0, 50.0),
    "lean_mass_kg": (25.0, 120.0),
    "resting_hr": (30.0, 120.0),
    "hrv": (5.0, 300.0),
    "spo2_avg": (70.0, 100.0),
    "respiratory_rate": (6.0, 30.0),
    "vo2_max": (20.0, 90.0),
    "steps": (0.0, 80000.0),
    "calories_total": (800.0, 8000.0),
    "skin_temp": (28.0, 40.0),
    "day_strain": (0.0, 21.0),
}

# Same-day spread: a reading this far from the day's median across all sources
# is a mis-entry, not a real fluctuation.
DISPERSION_TOLERANCE = {
    "weight_kg": 5.0,       # kg
    "body_fat_pct": 6.0,    # percentage points
}

# Maximum defensible change per day.
RATE_LIMITS = {
    "weight_kg": 1.5,       # kg/day
    "body_fat_pct": 2.0,    # percentage points/day
}

NUTRITION_BOUNDS = {
    "energy_kcal": (0.0, 12000.0),
    "protein_g": (0.0, 600.0),
    "carbs_g": (0.0, 1500.0),
    "fat_g": (0.0, 800.0),
}

SLEEP_BOUNDS = {
    "asleep_s": (30 * 60.0, 16 * 3600.0),
    "efficiency_pct": (0.0, 100.0),
}


def _flag(rows: list[dict], table: str, row_key, day, metric, value,
          reason: str, rule: str, severity: str = "warn") -> None:
    rows.append({
        "table_name": table, "row_key": str(row_key), "day": day,
        "metric": metric, "value": value, "reason": reason, "rule": rule,
        "severity": severity, "flagged_by": "auto", "resolved": False,
        "created_at": datetime.now(UTC),
    })


def check_body_composition(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT measurement_uid, source, method, day, weight_kg, "
            "       body_fat_pct, lean_mass_kg "
            "FROM fact_body_composition ORDER BY day"
        )
        rows = cur.fetchall()

    flags: list[dict] = []

    # 1. absolute plausibility
    for r in rows:
        for col in ("weight_kg", "body_fat_pct", "lean_mass_kg"):
            v = r[col]
            if v is None:
                continue
            lo, hi = PLAUSIBILITY[col]
            v = float(v)
            if not (lo <= v <= hi):
                _flag(flags, "fact_body_composition", r["measurement_uid"], r["day"],
                      col, v, f"{col}={v:g} outside plausible range {lo:g}-{hi:g}",
                      "plausibility", severity="reject")

    # 2. same-day dispersion
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    for day, group in by_day.items():
        for col, tol in DISPERSION_TOLERANCE.items():
            vals = [(float(g[col]), g) for g in group if g[col] is not None]
            if len(vals) < 3:
                continue
            med = statistics.median(v for v, _ in vals)
            for v, g in vals:
                if abs(v - med) > tol:
                    _flag(flags, "fact_body_composition", g["measurement_uid"], day,
                          col, v,
                          f"{col}={v:g} deviates {abs(v - med):.1f} from same-day "
                          f"median {med:g} across {len(vals)} readings",
                          "dispersion", severity="reject")

    # 3. rate of change, against the previous day that has a clean reading
    for col, limit in RATE_LIMITS.items():
        series = [(r["day"], float(r[col]), r) for r in rows if r[col] is not None]
        series.sort(key=lambda t: t[0])
        prev_day = prev_val = None
        flagged_keys = {f["row_key"] for f in flags}
        for day, val, r in series:
            if str(r["measurement_uid"]) in flagged_keys:
                continue
            if prev_day is not None:
                gap = max((day - prev_day).days, 1)
                delta = abs(val - prev_val) / gap
                if delta > limit:
                    _flag(flags, "fact_body_composition", r["measurement_uid"], day,
                          col, val,
                          f"{col} moved {delta:.2f}/day vs {prev_val:g} on "
                          f"{prev_day} (limit {limit:g}/day)",
                          "rate_of_change")
                    continue
            prev_day, prev_val = day, val

    return flags


def check_daily_metric(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT day, metric, source, value FROM fact_daily_metric")
        rows = cur.fetchall()
    flags: list[dict] = []
    for r in rows:
        bounds = PLAUSIBILITY.get(r["metric"])
        if not bounds or r["value"] is None:
            continue
        lo, hi = bounds
        v = float(r["value"])
        if not (lo <= v <= hi):
            _flag(flags, "fact_daily_metric",
                  f"{r['day']}|{r['metric']}|{r['source']}", r["day"],
                  r["metric"], v,
                  f"{r['metric']}={v:g} outside plausible range {lo:g}-{hi:g}",
                  "plausibility", severity="reject")
    return flags


def check_nutrition(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT day, source, energy_kcal, protein_g, carbs_g, fat_g "
                    "FROM fact_nutrition_daily")
        rows = cur.fetchall()
    flags: list[dict] = []
    for r in rows:
        for col, (lo, hi) in NUTRITION_BOUNDS.items():
            v = r[col]
            if v is None:
                continue
            v = float(v)
            if not (lo <= v <= hi):
                _flag(flags, "fact_nutrition_daily", f"{r['day']}|{r['source']}",
                      r["day"], col, v,
                      f"{col}={v:g} outside plausible range {lo:g}-{hi:g}",
                      "plausibility", severity="reject")
        # Macros that cannot add up to the stated calories, +/- 25%.
        kcal, p, c, f = (r["energy_kcal"], r["protein_g"], r["carbs_g"], r["fat_g"])
        if all(x is not None for x in (kcal, p, c, f)) and float(kcal) > 200:
            implied = float(p) * 4 + float(c) * 4 + float(f) * 9
            if implied > 0 and abs(implied - float(kcal)) / float(kcal) > 0.25:
                _flag(flags, "fact_nutrition_daily", f"{r['day']}|{r['source']}",
                      r["day"], "energy_kcal", float(kcal),
                      f"macros imply {implied:.0f} kcal but total says {float(kcal):.0f}",
                      "consistency")
    return flags


def check_sleep(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT sleep_uid, day, source, asleep_s, efficiency_pct, "
                    "is_nap FROM fact_sleep_session WHERE NOT is_nap")
        rows = cur.fetchall()
    flags: list[dict] = []
    for r in rows:
        for col, (lo, hi) in SLEEP_BOUNDS.items():
            v = r[col]
            if v is None:
                continue
            v = float(v)
            if not (lo <= v <= hi):
                _flag(flags, "fact_sleep_session", r["sleep_uid"], r["day"], col, v,
                      f"{col}={v:g} outside plausible range {lo:g}-{hi:g}",
                      "plausibility")
    return flags


def run_all(conn: psycopg.Connection) -> dict[str, int]:
    flags: list[dict] = []
    flags += check_body_composition(conn)
    flags += check_daily_metric(conn)
    flags += check_nutrition(conn)
    flags += check_sleep(conn)

    # Auto rules re-run from scratch each pass; clear their previous verdicts
    # so a corrected row stops being flagged. Manual flags are never touched.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM data_quality_flag WHERE flagged_by = 'auto'")

    # The unique index is (table_name, row_key, rule, metric); collapse first.
    dedup = {(f["table_name"], f["row_key"], f["rule"], f["metric"]): f
             for f in flags}
    n = upsert(conn, "data_quality_flag", list(dedup.values()),
               conflict=["table_name", "row_key", "rule", "metric"],
               update=["day", "value", "reason", "severity", "resolved", "created_at"])

    by_rule: dict[str, int] = {}
    for f in dedup.values():
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    log.info("quality.run", flags=n, **by_rule)
    return {"flags": n, **by_rule}
