"""Nutrition sources -> fact_nutrition_entry / fact_nutrition_daily.

Covers the sources that already have a home in the warehouse (Cronometer via
`fact_food_log`, the Cal AI app diary, Apple Health daily totals) plus the Cal
AI PDF report landed in `raw_import`.

Cal AI appears under two source keys on purpose:

    cal_ai       the in-app diary, captured from the device backup. Real
                 per-entry timestamps, richer payload.
    cal_ai_pdf   the exported Summary Report. Same underlying meals, but it
                 covers a wider date range than the last diary capture.

Keeping them separate means the overlap is resolved once, by precedence in
`dim_source_priority`, instead of double-counting a day's calories.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from lifeos_core.logging import get_logger
from unify.common import LOCAL_TZ, hash_uid, jsonb, local_day, num, upsert

log = get_logger(__name__)

_SUM_FIELDS = ("energy_kcal", "protein_g", "carbs_g", "net_carbs_g", "fiber_g",
               "sugar_g", "fat_g", "saturated_fat_g", "cholesterol_mg",
               "sodium_mg", "potassium_mg", "caffeine_mg", "alcohol_g")


def _rollup(entries: list[dict], source: str) -> list[dict]:
    """Sum per-item entries into one row per day."""
    daily: dict = {}
    for e in entries:
        d = daily.setdefault(e["day"], {
            **{f: 0.0 for f in _SUM_FIELDS},
            "entry_count": 0, "first": e["eaten_at"], "last": e["eaten_at"],
            "seen": {f: False for f in _SUM_FIELDS},
        })
        for f in _SUM_FIELDS:
            v = e.get(f)
            if v is not None:
                d[f] += float(v)
                d["seen"][f] = True
        d["entry_count"] += 1
        d["first"] = min(d["first"], e["eaten_at"])
        d["last"] = max(d["last"], e["eaten_at"])

    now = datetime.now(UTC)
    return [{
        "day": day, "source": source,
        # A nutrient nobody logged stays NULL rather than becoming a
        # confident-looking 0.0.
        **{f: (round(d[f], 2) if d["seen"][f] else None) for f in _SUM_FIELDS},
        "entry_count": d["entry_count"],
        "first_eaten_at": d["first"], "last_eaten_at": d["last"],
        "is_rollup": True, "micros": jsonb({}), "updated_at": now,
    } for day, d in daily.items()]


# ---------------------------------------------------------------------------
# fact_food_log (Cronometer + Cal AI app diary)
# ---------------------------------------------------------------------------

# `fact_food_log` is filled by the Go `cronometer-export` binary (GWT-RPC).
# `unify.sources.cronometer` fills the same days from the mobile JSON API.
# They are the same meals seen through two pipes, so they get distinct source
# keys and precedence decides -- otherwise a day's calories would be counted
# twice in the rollup.
_SOURCE_RENAME = {"calai": "cal_ai", "cronometer": "cronometer_csv"}


def project_food_log(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_food_log")
        raw = cur.fetchall()

    by_source: dict[str, list[dict]] = {}
    for f in raw:
        source = _SOURCE_RENAME.get(f["source"], f["source"])
        entry = {
            "entry_uid": hash_uid(source, f["source_row_hash"]),
            "source": source,
            "source_entry_id": f["source_row_hash"],
            "eaten_at": f["eaten_at"],
            "day": f["day"] or local_day(f["eaten_at"]),
            "meal": (f["meal_group"] or "unknown").lower(),
            "food_name": f["food_name"],
            "brand": None,
            "amount": f["amount"],
            "unit": f["unit"],
            "energy_kcal": f["energy_kcal"],
            "protein_g": f["protein_g"],
            "carbs_g": f["carbs_g"],
            "net_carbs_g": f["net_carbs_g"],
            "fiber_g": f["fiber_g"],
            "sugar_g": f["sugar_g"],
            "fat_g": f["fat_g"],
            "saturated_fat_g": f["saturated_fat_g"],
            "cholesterol_mg": None,
            "sodium_mg": f["sodium_mg"],
            "potassium_mg": f["potassium_mg"],
            "caffeine_mg": f["caffeine_mg"],
            "alcohol_g": f["alcohol_g"],
            "micros": jsonb(f["micros"] or {}),
            "raw_id": None,
            "payload": jsonb({}),
            "updated_at": datetime.now(UTC),
        }
        by_source.setdefault(source, []).append(entry)

    n_e = n_d = 0
    for source, entries in by_source.items():
        n_e += upsert(conn, "fact_nutrition_entry", entries, conflict=["entry_uid"])
        n_d += upsert(conn, "fact_nutrition_daily", _rollup(entries, source),
                      conflict=["day", "source"])
    log.info("nutrition.project.food_log", entries=n_e, days=n_d,
             sources=sorted(by_source))
    return {"entries": n_e, "days": n_d}


# ---------------------------------------------------------------------------
# Vendor-reported daily totals (fact_food_daily, Apple Health)
# ---------------------------------------------------------------------------

def project_daily_tables(conn: psycopg.Connection) -> int:
    now = datetime.now(UTC)
    rows = []
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_food_daily")
        for d in cur.fetchall():
            source = _SOURCE_RENAME.get(d["source"], d["source"])
            rows.append({
                "day": d["day"], "source": source,
                "energy_kcal": d["energy_kcal"], "protein_g": d["protein_g"],
                "carbs_g": d["carbs_g"], "net_carbs_g": d["net_carbs_g"],
                "fiber_g": d["fiber_g"], "sugar_g": None, "fat_g": d["fat_g"],
                "saturated_fat_g": d["saturated_fat_g"], "cholesterol_mg": None,
                "sodium_mg": d["sodium_mg"], "potassium_mg": None,
                "caffeine_mg": d["caffeine_mg"], "alcohol_g": d["alcohol_g"],
                "entry_count": 0, "first_eaten_at": None, "last_eaten_at": None,
                "is_rollup": False, "micros": jsonb(d["micros"] or {}),
                "updated_at": now,
            })
        cur.execute("SELECT * FROM fact_food_daily_apple_health")
        for d in cur.fetchall():
            rows.append({
                "day": d["day"], "source": "apple_health",
                "energy_kcal": d["energy_kcal"], "protein_g": d["protein_g"],
                "carbs_g": d["carbs_g"], "net_carbs_g": None,
                "fiber_g": d["fiber_g"], "sugar_g": None, "fat_g": d["fat_g"],
                "saturated_fat_g": None, "cholesterol_mg": None,
                "sodium_mg": d["sodium_mg"], "potassium_mg": None,
                "caffeine_mg": None, "alcohol_g": None,
                "entry_count": 0, "first_eaten_at": None, "last_eaten_at": None,
                "is_rollup": False,
                "micros": jsonb({"calcium_mg": float(d["calcium_mg"]) if d["calcium_mg"] else None,
                                 "magnesium_mg": float(d["magnesium_mg"]) if d["magnesium_mg"] else None}),
                "updated_at": now,
            })

    # A day present in both fact_food_daily and its own rollup would collide;
    # the rollup from project_food_log wins because it runs after.
    dedup = {(r["day"], r["source"]): r for r in rows}
    n = upsert(conn, "fact_nutrition_daily", list(dedup.values()),
               conflict=["day", "source"],
               # Don't let a vendor daily-total row clobber a richer rollup.
               update=["energy_kcal", "protein_g", "carbs_g", "net_carbs_g",
                       "fiber_g", "fat_g", "saturated_fat_g", "sodium_mg",
                       "caffeine_mg", "alcohol_g", "micros", "updated_at"])
    log.info("nutrition.project.daily_tables", rows=n)
    return n


# ---------------------------------------------------------------------------
# Cal AI PDF report
# ---------------------------------------------------------------------------

def project_calai_pdf(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, natural_key, occurred_on, payload FROM raw_import "
                    "WHERE source = 'cal_ai' AND entity = 'food_entry'")
        raw = cur.fetchall()

    entries = []
    for r in raw:
        e = r["payload"]
        eaten = datetime.fromisoformat(e["eaten_at"]).replace(tzinfo=LOCAL_TZ)
        entries.append({
            "entry_uid": hash_uid("cal_ai_pdf", r["natural_key"]),
            "source": "cal_ai_pdf",
            "source_entry_id": r["natural_key"],
            "eaten_at": eaten,
            "day": r["occurred_on"],
            "meal": None,
            "food_name": e["food_name"],
            "brand": None, "amount": None, "unit": None,
            "energy_kcal": num(e.get("energy_kcal")),
            "protein_g": num(e.get("protein_g")),
            "carbs_g": num(e.get("carbs_g")),
            "net_carbs_g": None,
            "fiber_g": num(e.get("fiber_g")),
            "sugar_g": num(e.get("sugar_g")),
            "fat_g": num(e.get("fat_g")),
            "saturated_fat_g": None, "cholesterol_mg": None,
            "sodium_mg": num(e.get("sodium_mg")),
            "potassium_mg": None, "caffeine_mg": None, "alcohol_g": None,
            "micros": jsonb({}),
            "raw_id": r["id"], "payload": jsonb({"seq": e.get("seq")}),
            "updated_at": datetime.now(UTC),
        })

    n_e = upsert(conn, "fact_nutrition_entry", entries, conflict=["entry_uid"])
    n_d = upsert(conn, "fact_nutrition_daily", _rollup(entries, "cal_ai_pdf"),
                 conflict=["day", "source"])

    # The report's own TOTAL line is the app's number; keep it as a check
    # against our rollup rather than as a competing source of truth.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.occurred_on,
                   (r.payload->>'kcal_eaten')::numeric AS stated,
                   n.energy_kcal                       AS rolled_up
              FROM raw_import r
              LEFT JOIN fact_nutrition_daily n
                     ON n.day = r.occurred_on AND n.source = 'cal_ai_pdf'
             WHERE r.source = 'cal_ai' AND r.entity = 'daily_total'
            """
        )
        mismatches = sum(
            1 for row in cur.fetchall()
            if row["stated"] and row["rolled_up"] is not None
            and abs(float(row["rolled_up"]) - float(row["stated"])) > 2
        )
    if mismatches:
        log.warning("nutrition.calai_pdf.total_mismatch", days=mismatches)

    log.info("nutrition.project.calai_pdf", entries=n_e, days=n_d,
             total_mismatches=mismatches)
    return {"entries": n_e, "days": n_d, "total_mismatches": mismatches}


def project_calai_pdf_weight(conn: psycopg.Connection) -> int:
    from unify.common import lb_to_kg, uid
    rows = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, occurred_on, payload FROM raw_import "
                    "WHERE source = 'cal_ai' AND entity = 'weight'")
        for r in cur.fetchall():
            lb = num(r["payload"].get("weight_lb"))
            if not lb or not r["occurred_on"]:
                continue
            ts = datetime.combine(r["occurred_on"], datetime.min.time(), tzinfo=LOCAL_TZ)
            rows.append({
                "measurement_uid": uid("cal_ai", "manual", int(ts.timestamp())),
                "source": "cal_ai", "method": "manual",
                "measured_at": ts, "day": r["occurred_on"],
                "weight_kg": lb_to_kg(lb),
                "body_fat_pct": None, "lean_mass_kg": None, "fat_mass_kg": None,
                "bone_mineral_kg": None, "visceral_fat": None, "bmi": None,
                "muscle_mass_kg": None, "body_water_pct": None,
                "region": jsonb({}), "confidence": 0.5,
                "raw_id": r["id"], "payload": jsonb({"weight_lb": lb}),
                "updated_at": datetime.now(UTC),
            })
    n = upsert(conn, "fact_body_composition", rows, conflict=["measurement_uid"])
    log.info("nutrition.project.calai_weight", rows=n)
    return n


def project_all(conn: psycopg.Connection) -> dict:
    out = {}
    out["daily_tables"] = project_daily_tables(conn)
    out.update({f"log_{k}": v for k, v in project_food_log(conn).items()})
    out.update({f"pdf_{k}": v for k, v in project_calai_pdf(conn).items()})
    out["pdf_weight"] = project_calai_pdf_weight(conn)
    return out
