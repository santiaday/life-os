"""Full-history Cronometer backfill over the mobile JSON API.

The existing pipeline (`exporter.py` -> the `cronometer-export` Go binary ->
GWT-RPC) only runs inside the container and only ever asked for a rolling
window, so `fact_food_log` starts at 2026-03-28 even though the account is
older. This module is a pure-Python path that can walk any date range from
anywhere, using the same mobile API the Android app talks to.

Shape of the data:

    get_diary(day) -> {'diary': [ {type: 'Serving', foodId, grams, time, ...},
                                  {type: 'Biometric', metricId, amount, ...} ],
                       'summary': {'consumed': {...}}}

Servings reference a food by id and carry only grams; the macros live on the
food record, per 100 g. `get_food` results are cached in `raw_import` so a
re-run costs one request per *new* food rather than one per serving.

Everything lands in raw_import; `unify.sources.cronometer` projects it.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify.common import jsonb, upsert

log = get_logger(__name__)

SOURCE = "cronometer"

# Cronometer uses USDA nutrient ids. Amounts on a food record are per 100 g.
NUTRIENT_IDS = {
    208: "energy_kcal",
    203: "protein_g",
    204: "fat_g",
    205: "carbs_g",
    291: "fiber_g",
    269: "sugar_g",
    606: "saturated_fat_g",
    605: "trans_fat_g",
    645: "mono_fat_g",
    646: "poly_fat_g",
    601: "cholesterol_mg",
    307: "sodium_mg",
    306: "potassium_mg",
    262: "caffeine_mg",
    221: "alcohol_g",
    301: "calcium_mg",
    303: "iron_mg",
    304: "magnesium_mg",
    305: "phosphorus_mg",
    309: "zinc_mg",
    317: "selenium_ug",
    312: "copper_mg",
    315: "manganese_mg",
    320: "vitamin_a_ug",
    401: "vitamin_c_mg",
    323: "vitamin_e_mg",
    328: "vitamin_d_ug",
    430: "vitamin_k_ug",
    404: "thiamine_mg",
    405: "riboflavin_mg",
    406: "niacin_mg",
    410: "pantothenic_acid_mg",
    415: "vitamin_b6_mg",
    417: "folate_ug",
    418: "vitamin_b12_ug",
    421: "choline_mg",
    318: "vitamin_a_iu",
}


def _cached_food_ids(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT natural_key FROM raw_import "
                    "WHERE source = %s AND entity = 'food'", [SOURCE])
        return {int(r["natural_key"]) for r in cur.fetchall()}


def _days_with_content(conn) -> set[str]:
    """Days already landed with at least one diary entry. Used by
    --skip-known so a re-run only spends requests on days we still have
    nothing for -- successive runs converge instead of re-paying for the
    whole range."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT natural_key FROM raw_import "
            "WHERE source = %s AND entity = 'diary' "
            "  AND jsonb_array_length(COALESCE(payload->'diary', '[]'::jsonb)) > 0",
            [SOURCE],
        )
        return {r["natural_key"] for r in cur.fetchall()}


def backfill(start: date, end: date, *, delay: float = 1.2,
             skip_known: bool = False) -> dict:
    """Walk [start, end] day by day. Returns counts.

    Cronometer rate-limits the mobile API and, when throttled, answers with
    HTTP 200 and an *empty* diary rather than an error -- which looks exactly
    like a day with nothing logged. `delay` is therefore not optional politeness;
    without it roughly a third of days come back silently blank.
    """
    import time as _time

    from ingest_cronometer.mobile_client import CronometerMobileClient

    days_with_food = 0
    days_scanned = 0
    days_skipped = 0
    servings = 0
    biometrics = 0
    new_foods = 0

    with CronometerMobileClient() as client, tx() as conn:
        known_foods = _cached_food_ids(conn)
        already = _days_with_content(conn) if skip_known else set()
        day = start
        pending_days: list[dict] = []
        pending_foods: list[dict] = []

        while day <= end:
            if skip_known and day.isoformat() in already:
                days_skipped += 1
                day += timedelta(days=1)
                continue
            days_scanned += 1
            if delay:
                _time.sleep(delay)
            try:
                diary = client.get_diary(day)
            except Exception as e:
                log.warning("cronometer.history.day_failed", day=str(day), error=str(e))
                day += timedelta(days=1)
                continue

            entries = diary.get("diary") or []
            srv = [e for e in entries if e.get("type") == "Serving"]
            bio = [e for e in entries if e.get("type") == "Biometric"]
            servings += len(srv)
            biometrics += len(bio)
            if srv:
                days_with_food += 1

            if entries or (diary.get("summary") or {}).get("consumed", {}).get("total"):
                pending_days.append({
                    "source": SOURCE, "entity": "diary",
                    "natural_key": day.isoformat(),
                    "occurred_on": day.isoformat(),
                    "payload": jsonb({"diary": entries,
                                      "summary": diary.get("summary")}),
                    "file_name": "mobile_api",
                })

            for s in srv:
                fid = s.get("foodId")
                if fid and fid not in known_foods:
                    try:
                        food = client.get_food(fid)
                    except Exception as e:
                        log.warning("cronometer.history.food_failed",
                                    food_id=fid, error=str(e))
                        continue
                    known_foods.add(fid)
                    new_foods += 1
                    pending_foods.append({
                        "source": SOURCE, "entity": "food",
                        "natural_key": str(fid), "occurred_on": None,
                        "payload": jsonb(food), "file_name": "mobile_api",
                    })

            # Flush periodically so a long walk doesn't hold one giant txn.
            if len(pending_days) >= 60 or len(pending_foods) >= 60:
                upsert(conn, "raw_import", pending_days,
                       conflict=["source", "entity", "natural_key"],
                       update=["occurred_on", "payload", "file_name", "imported_at"])
                upsert(conn, "raw_import", pending_foods,
                       conflict=["source", "entity", "natural_key"],
                       update=["payload", "file_name", "imported_at"])
                conn.commit()
                pending_days, pending_foods = [], []
                log.info("cronometer.history.progress", day=str(day),
                         servings=servings, foods=new_foods)

            day += timedelta(days=1)

        upsert(conn, "raw_import", pending_days,
               conflict=["source", "entity", "natural_key"],
               update=["occurred_on", "payload", "file_name", "imported_at"])
        upsert(conn, "raw_import", pending_foods,
               conflict=["source", "entity", "natural_key"],
               update=["payload", "file_name", "imported_at"])

    return {
        "days_scanned": days_scanned,
        "days_skipped": days_skipped,
        "days_with_food": days_with_food,
        "servings": servings,
        "biometrics": biometrics,
        "new_foods": new_foods,
    }


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(
        description="Backfill Cronometer history via the mobile API.")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=date.today().isoformat(), help="YYYY-MM-DD")
    p.add_argument("--delay", type=float, default=1.2,
                   help="Seconds between day requests. Below ~1s Cronometer "
                        "throttles and returns empty diaries with HTTP 200.")
    p.add_argument("--skip-known", action="store_true",
                   help="Skip days already landed with at least one entry.")
    args = p.parse_args(argv)

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    with ingestion_run(SOURCE, "history",
                       start=start.isoformat(), end=end.isoformat()) as run:
        result = backfill(start, end, delay=args.delay, skip_known=args.skip_known)
        run.fetched(result["days_scanned"])
        run.upserted(result["servings"] + result["biometrics"])
        run.add_metadata(**result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
