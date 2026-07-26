"""Zero (fasting app) data-export loader.

`biodata.json` is the densest weight series in the warehouse and the only
source covering Aug-Nov 2023. Zero has no public API, so this is a one-shot
historical load; nothing keeps it current.

Known defect in the export: every row in `caloric_intake_data` has its
timestamp zeroed to 0001-01-01, so the 2,620 nutrition records cannot be
placed on a calendar. They are still landed in raw_import (the raw layer is
immutable and the vendor may fix the export later) but `unify` refuses to
project them.

All Zero timestamps are UTC.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify.common import jsonb, upsert

log = get_logger(__name__)

SOURCE = "zero"

# biodata.json key -> (entity, natural-key field, timestamp field)
DATASETS: dict[str, tuple[str, str, str]] = {
    "weight_data":         ("weight", "id", "log_dtm"),
    "rhr_data":            ("resting_hr", "id", "log_dtm"),
    "sleep_data":          ("sleep", "ID", "SleepStartDTM"),
    "active_minutes_data": ("active_minutes", "ID", "StartDTM"),
    "fast_data":           ("fast", "FastID", "StartDTM"),
    "glucose_data":        ("glucose", "id", "log_dtm"),
    "caloric_intake_data": ("caloric_intake", "id", "timestamp"),
    "mood_data":           ("mood", "id", "log_dtm"),
    "mindful_minutes_data": ("mindful_minutes", "id", "log_dtm"),
    "meal_data":           ("meal", "id", "log_dtm"),
    "water_log_data":      ("water", "id", "log_dtm"),
}


def _find_biodata(root: Path) -> Path:
    direct = root / "biodata.json"
    if direct.exists():
        return direct
    hits = glob.glob(str(root / "**" / "biodata.json"), recursive=True)
    if not hits:
        raise SystemExit(f"no biodata.json found under {root}")
    return Path(hits[0])


def load_all(export_root: str, only: list[str] | None = None) -> dict[str, int]:
    path = _find_biodata(Path(export_root).expanduser())
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)

    out: dict[str, int] = {}
    for src_key, (entity, id_field, ts_field) in DATASETS.items():
        if only and entity not in only:
            continue
        records = blob.get(src_key) or []
        if not records:
            out[entity] = 0
            continue
        with ingestion_run(SOURCE, entity, mode="file_export") as run, tx() as conn:
            rows = []
            for i, r in enumerate(records):
                ts = str(r.get(ts_field) or "")
                # 0001-01-01 is Zero's null sentinel, not a real date.
                day = ts[:10] if ts[:4].isdigit() and ts[:4] != "0001" else None
                rows.append({
                    "source": SOURCE,
                    "entity": entity,
                    "natural_key": str(r.get(id_field) or f"{entity}:{i}"),
                    "occurred_on": day,
                    "payload": jsonb(r),
                    "file_name": path.name,
                })
            n = upsert(conn, "raw_import", rows,
                       conflict=["source", "entity", "natural_key"],
                       update=["occurred_on", "payload", "file_name", "imported_at"])
            run.fetched(len(records))
            run.upserted(n)
            out[entity] = n
            log.info("zero.load", entity=entity, rows=n)
    return out


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(description="Load a Zero fasting-app data export.")
    p.add_argument("--dir", required=True)
    p.add_argument("--only", action="append")
    args = p.parse_args(argv)
    print(json.dumps(load_all(args.dir, args.only), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
