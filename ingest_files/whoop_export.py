"""Whoop official CSV data-export loader.

Whoop's in-app "Download my data" export reaches back to the first day the
strap was worn (2023-11-16 here), which is 22 months earlier than any of the
API cursors ever asked for. It is also the only source that carries HR-zone
percentages for historical workouts.

Files, all in one flat directory:
    physiological_cycles.csv   one row per Whoop cycle (a cycle is NOT a day)
    sleeps.csv                 one row per sleep, naps included
    workouts.csv               one row per workout, with HR zone percentages
    journal_entries.csv        one row per behaviour question per cycle

Timestamps are naive local wall-clock strings; the true offset is in the
`Cycle timezone` column (e.g. "UTC-04:00"). We reconstruct real instants from
the pair rather than assuming America/New_York, because several of these rows
were recorded while travelling.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify.common import hash_uid, jsonb, parse_offset, parse_ts, upsert

log = get_logger(__name__)

SOURCE = "whoop_export"


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _land(conn, entity: str, records: list[tuple[str, str | None, dict]], file_name: str) -> int:
    rows = [{"source": SOURCE, "entity": entity, "natural_key": nk,
             "occurred_on": day, "payload": jsonb(payload), "file_name": file_name}
            for nk, day, payload in records]
    return upsert(conn, "raw_import", rows,
                  conflict=["source", "entity", "natural_key"],
                  update=["occurred_on", "payload", "file_name", "imported_at"])


def load_cycles(conn, root: Path) -> int:
    path = root / "physiological_cycles.csv"
    if not path.exists():
        return 0
    recs = []
    for r in _read(path):
        tz = parse_offset(r.get("Cycle timezone"))
        start = parse_ts(r.get("Cycle start time"), tz)
        if not start:
            continue
        recs.append((r["Cycle start time"], start.astimezone(tz).date().isoformat(), r))
    n = _land(conn, "cycle", recs, path.name)
    log.info("whoop_export.cycles", rows=n)
    return n


def load_sleeps(conn, root: Path) -> int:
    path = root / "sleeps.csv"
    if not path.exists():
        return 0
    recs = []
    for r in _read(path):
        tz = parse_offset(r.get("Cycle timezone"))
        onset = parse_ts(r.get("Sleep onset"), tz)
        wake = parse_ts(r.get("Wake onset"), tz)
        if not onset or not wake:
            continue
        recs.append((f"{r.get('Sleep onset')}|{r.get('Wake onset')}",
                     wake.astimezone(tz).date().isoformat(), r))
    n = _land(conn, "sleep", recs, path.name)
    log.info("whoop_export.sleeps", rows=n)
    return n


def load_workouts(conn, root: Path) -> int:
    path = root / "workouts.csv"
    if not path.exists():
        return 0
    recs = []
    for r in _read(path):
        tz = parse_offset(r.get("Cycle timezone"))
        start = parse_ts(r.get("Workout start time"), tz)
        if not start:
            continue
        recs.append((f"{r.get('Workout start time')}|{r.get('Activity name')}",
                     start.astimezone(tz).date().isoformat(), r))
    n = _land(conn, "workout", recs, path.name)
    log.info("whoop_export.workouts", rows=n)
    return n


def load_journal(conn, root: Path) -> int:
    path = root / "journal_entries.csv"
    if not path.exists():
        return 0
    recs = []
    for r in _read(path):
        tz = parse_offset(r.get("Cycle timezone"))
        start = parse_ts(r.get("Cycle start time"), tz)
        if not start:
            continue  # a few thousand rows have no cycle attached; unusable
        day = start.astimezone(tz).date().isoformat()
        key = hash_uid("j", r.get("Cycle start time"), r.get("Question text"))
        recs.append((key, day, r))
    n = _land(conn, "journal", recs, path.name)
    log.info("whoop_export.journal", rows=n)
    return n


LOADERS = {
    "cycle": load_cycles,
    "sleep": load_sleeps,
    "workout": load_workouts,
    "journal": load_journal,
}


def load_all(export_root: str, only: list[str] | None = None) -> dict[str, int]:
    root = Path(export_root).expanduser()
    if not (root / "physiological_cycles.csv").exists():
        raise SystemExit(f"{root} does not look like a Whoop export "
                         f"(no physiological_cycles.csv)")
    out: dict[str, int] = {}
    for name, fn in LOADERS.items():
        if only and name not in only:
            continue
        with ingestion_run(SOURCE, name, mode="file_export") as run, tx() as conn:
            n = fn(conn, root)
            run.fetched(n)
            run.upserted(n)
            out[name] = n
    return out


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(description="Load a Whoop CSV data export.")
    p.add_argument("--dir", required=True)
    p.add_argument("--only", action="append", choices=sorted(LOADERS))
    args = p.parse_args(argv)
    result = load_all(args.dir, args.only)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
