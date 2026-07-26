"""Lose It! data-export loader.

The export is a directory of flat CSVs. It is materially richer than anything
the Lose It web API returns (the reverse-engineered GWT endpoint gives daily
totals, not per-item macros), so the export is the authoritative historical
load and `ingest_loseit` only keeps recent days current.

Dates are MM/DD/YYYY with no time component. Food rows carry a meal name
instead, so `unify` places them at a representative hour per meal -- the
ordering within a day is meaningful, the exact minute is not.

`Deleted` is a tombstone column: rows marked deleted were removed by the user
and must not count toward daily totals.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify.common import hash_uid, jsonb, upsert

log = get_logger(__name__)

SOURCE = "loseit"

# file -> (entity, [columns forming the natural key])
DATASETS: dict[str, tuple[str, list[str]]] = {
    "food-logs.csv":            ("food_log", ["Date", "Name", "Meal", "Quantity", "Units", "Calories"]),
    "daily-calorie-summary.csv": ("daily_summary", ["Date"]),
    "weights.csv":              ("weight", ["Date"]),
    "exercise-logs.csv":        ("exercise_log", ["Date", "Name", "Quantity", "Units", "Calories"]),
    "exercise-minutes.csv":     ("exercise_minutes", ["Date"]),
    "sleep.csv":                ("sleep", ["Date"]),
    "steps.csv":                ("steps", ["Date"]),
    "protein.csv":              ("protein", ["Date"]),
    "fat.csv":                  ("fat", ["Date"]),
    "fiber.csv":                ("fiber", ["Date"]),
    "sugar.csv":                ("sugar", ["Date"]),
    "garmin-calories.csv":      ("garmin_calories", ["Date"]),
    "water-intake.csv":         ("water", ["Date"]),
    "custom-foods.csv":         ("custom_food", ["UniqueId"]),
    "recipes.csv":              ("recipe", ["UniqueId"]),
    "calorie-bonus.csv":        ("calorie_bonus", ["Date"]),
    "fasting-logs.csv":         ("fast", ["Date"]),
    "profile.csv":              ("profile", ["Name"]),
}


def parse_date(v: str | None) -> str | None:
    """'10/04/2023' -> '2023-10-04'. Returns None for blanks and junk."""
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def load_all(export_root: str, only: list[str] | None = None) -> dict[str, int]:
    root = Path(export_root).expanduser()
    out: dict[str, int] = {}
    for fname, (entity, key_cols) in DATASETS.items():
        if only and entity not in only:
            continue
        path = root / fname
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            records = list(csv.DictReader(fh))
        if not records:
            out[entity] = 0
            continue
        with ingestion_run(SOURCE, entity, mode="file_export") as run, tx() as conn:
            rows = []
            # Two identical food rows on one day are legitimate (ate the same
            # thing twice at one meal). Disambiguate by occurrence count rather
            # than file position, so inserting a row earlier in a later export
            # doesn't renumber -- and therefore duplicate -- everything after it.
            seen: dict[tuple, int] = {}
            for r in records:
                day = parse_date(r.get("Date")) if "Date" in r else None
                key_tuple = tuple(r.get(c) for c in key_cols)
                occurrence = seen.get(key_tuple, 0)
                seen[key_tuple] = occurrence + 1
                nk = hash_uid("k", *key_tuple, occurrence)
                rows.append({
                    "source": SOURCE,
                    "entity": entity,
                    "natural_key": nk,
                    "occurred_on": day,
                    "payload": jsonb(r),
                    "file_name": fname,
                })
            n = upsert(conn, "raw_import", rows,
                       conflict=["source", "entity", "natural_key"],
                       update=["occurred_on", "payload", "file_name", "imported_at"])
            run.fetched(len(records))
            run.upserted(n)
            out[entity] = n
            log.info("loseit.load", entity=entity, rows=n)
    return out


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(description="Load a Lose It! data export directory.")
    p.add_argument("--dir", required=True)
    p.add_argument("--only", action="append")
    args = p.parse_args(argv)
    print(json.dumps(load_all(args.dir, args.only), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
