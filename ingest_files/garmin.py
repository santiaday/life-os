"""Garmin Connect data-export loader.

Reads the GDPR export tree (the `DI_CONNECT/...` directory Garmin mails you)
and lands every useful entity in `raw_import`.

Unit notes -- Garmin's export is the worst offender in the whole warehouse and
every one of these was confirmed against the actual file, not assumed:

    duration, elapsedDuration, movingDuration   milliseconds
    hrTimeInZone_0..6                           milliseconds
    distance, elevationGain/Loss, min/maxElevation   centimeters
    avgSpeed, maxSpeed                          centimeters per millisecond
    summarizedExerciseSets.volume / maxWeight   grams
    summarizedExerciseSets.duration             milliseconds
    userBioMetrics.weight.weight                grams
    beginTimestamp / startTimeGmt               epoch milliseconds, true UTC
    startTimeLocal                              epoch milliseconds of the LOCAL
                                                wall clock (i.e. already shifted;
                                                do not treat as a real instant)
    calories, bmrCalories                       kcal

Conversion happens in `unify.sources.garmin`, not here -- raw stays raw.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
from typing import Any

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify.common import jsonb, upsert

log = get_logger(__name__)

SOURCE = "garmin"


def _land(conn, entity: str, records: list[tuple[str, str | None, dict]],
          file_name: str) -> int:
    """records = [(natural_key, occurred_on_iso, payload), ...]"""
    rows = [
        {
            "source": SOURCE,
            "entity": entity,
            "natural_key": str(nk),
            "occurred_on": day,
            "payload": jsonb(payload),
            "file_name": file_name,
        }
        for nk, day, payload in records
    ]
    return upsert(conn, "raw_import", rows,
                  conflict=["source", "entity", "natural_key"],
                  update=["occurred_on", "payload", "file_name", "imported_at"])


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _epoch_ms_to_date(ms) -> str | None:
    if not ms:
        return None
    from datetime import UTC, datetime

    from unify.common import LOCAL_TZ
    return (datetime.fromtimestamp(float(ms) / 1000, UTC)
            .astimezone(LOCAL_TZ).date().isoformat())


# ---------------------------------------------------------------------------
# Entity loaders
# ---------------------------------------------------------------------------

def load_activities(conn, root: Path) -> int:
    total = 0
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Fitness"
                                      / "*summarizedActivities.json"))):
        blob = _read_json(path)
        acts = []
        for chunk in blob if isinstance(blob, list) else [blob]:
            acts.extend(chunk.get("summarizedActivitiesExport") or [])
        recs = [
            (a["activityId"], _epoch_ms_to_date(a.get("startTimeGmt") or a.get("beginTimestamp")), a)
            for a in acts
        ]
        total += _land(conn, "activity", recs, os.path.basename(path))
        log.info("garmin.activities", file=os.path.basename(path), rows=len(recs))
    return total


def load_sleep(conn, root: Path) -> int:
    total = 0
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Wellness"
                                      / "*sleepData.json"))):
        rows = _read_json(path)
        recs = []
        for r in rows:
            cal = r.get("calendarDate")
            if not cal:
                continue  # a handful of export rows are just {"retro": false}
            recs.append((cal, cal, r))
        total += _land(conn, "sleep", recs, os.path.basename(path))
        log.info("garmin.sleep", file=os.path.basename(path), rows=len(recs))
    return total


def load_daily(conn, root: Path) -> int:
    """UDS = the all-day wellness summary: steps, calories, RHR, stress,
    body battery, intensity minutes, SpO2."""
    total = 0
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Aggregator"
                                      / "UDSFile_*.json"))):
        rows = _read_json(path)
        recs = [(r["calendarDate"], r["calendarDate"], r)
                for r in rows if r.get("calendarDate")]
        total += _land(conn, "daily", recs, os.path.basename(path))
        log.info("garmin.daily", file=os.path.basename(path), rows=len(recs))
    return total


def load_biometrics(conn, root: Path) -> int:
    """userBioMetrics: weight (grams), BMI, body fat %, back to 2023-08."""
    total = 0
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Wellness"
                                      / "*userBioMetrics.json"))):
        rows = _read_json(path)
        recs = []
        for r in rows:
            cal = ((r.get("metaData") or {}).get("calendarDate") or "")[:10]
            seq = (r.get("metaData") or {}).get("sequence") or r.get("version")
            if not cal:
                continue
            recs.append((f"{cal}:{seq}", cal, r))
        total += _land(conn, "biometrics", recs, os.path.basename(path))
        log.info("garmin.biometrics", file=os.path.basename(path), rows=len(recs))
    return total


def load_metrics(conn, root: Path) -> int:
    """VO2max, fitness age, training status, endurance/hill scores, acute load."""
    total = 0
    metric_dir = root / "DI_CONNECT" / "DI-Connect-Metrics"
    patterns = {
        "vo2max": "MetricsMaxMetData_*.json",
        "training_status": "TrainingHistory_*.json",
        "acute_load": "MetricsAcuteTrainingLoad_*.json",
        "endurance_score": "EnduranceScore_*.json",
        "hill_score": "HillScore_*.json",
        "training_readiness": "TrainingReadinessDTO_*.json",
        "race_prediction": "RunRacePredictions_*.json",
    }
    for entity, pat in patterns.items():
        for path in sorted(glob.glob(str(metric_dir / pat))):
            try:
                rows = _read_json(path)
            except Exception as e:  # a couple of these files can be empty
                log.warning("garmin.metric.unreadable", file=path, error=str(e))
                continue
            if not isinstance(rows, list):
                rows = [rows]
            recs = []
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                # `timestamp` is an ISO string on some metric types and epoch
                # millis on others, so coerce before slicing.
                stamp = (r.get("calendarDate") or r.get("timestamp")
                         or r.get("asOfDateGmt"))
                if isinstance(stamp, (int, float)):
                    cal = _epoch_ms_to_date(stamp)
                else:
                    cal = (stamp or "")[:10] or None
                recs.append((f"{cal or 'na'}:{entity}:{i}:{os.path.basename(path)}", cal, r))
            total += _land(conn, entity, recs, os.path.basename(path))
    # fitness age lives outside DI-Connect-Metrics
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Wellness"
                                      / "*fitnessAgeData.json"))):
        rows = _read_json(path)
        recs = [((r.get("asOfDateGmt") or "")[:10], (r.get("asOfDateGmt") or "")[:10] or None, r)
                for r in rows if r.get("asOfDateGmt")]
        total += _land(conn, "fitness_age", recs, os.path.basename(path))
    log.info("garmin.metrics", rows=total)
    return total


def load_personal_records(conn, root: Path) -> int:
    total = 0
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Fitness"
                                      / "*personalRecord.json"))):
        blob = _read_json(path)
        prs = []
        for chunk in blob if isinstance(blob, list) else [blob]:
            prs.extend(chunk.get("personalRecords") or [])
        recs = [(p["personalRecordId"], (p.get("createdDate") or "")[:10] or None, p) for p in prs]
        total += _land(conn, "personal_record", recs, os.path.basename(path))
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Fitness"
                                      / "*benchmarks.json"))):
        rows = _read_json(path)
        recs = [(b["id"], (b.get("dateSet") or "")[:10] or None, b) for b in rows]
        total += _land(conn, "benchmark", recs, os.path.basename(path))
    log.info("garmin.personal_records", rows=total)
    return total


def load_hydration(conn, root: Path) -> int:
    total = 0
    for path in sorted(glob.glob(str(root / "DI_CONNECT" / "DI-Connect-Aggregator"
                                      / "HydrationLogFile_*.json"))):
        rows = _read_json(path)
        recs = []
        for r in rows:
            cal = r.get("calendarDate")
            key = ((r.get("uuid") or {}).get("uuid")
                   if isinstance(r.get("uuid"), dict) else r.get("uuid"))
            if not cal:
                continue
            recs.append((key or f"{cal}:{r.get('activityId')}", cal, r))
        total += _land(conn, "hydration", recs, os.path.basename(path))
    log.info("garmin.hydration", rows=total)
    return total


LOADERS = {
    "activity": load_activities,
    "sleep": load_sleep,
    "daily": load_daily,
    "biometrics": load_biometrics,
    "metrics": load_metrics,
    "personal_record": load_personal_records,
    "hydration": load_hydration,
}


def load_all(export_root: str, only: list[str] | None = None) -> dict[str, int]:
    root = Path(export_root).expanduser()
    if not (root / "DI_CONNECT").exists():
        raise SystemExit(f"{root} does not look like a Garmin export "
                         f"(no DI_CONNECT directory)")
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
    p = argparse.ArgumentParser(description="Load a Garmin Connect data export.")
    p.add_argument("--dir", required=True, help="Path to the unzipped export root")
    p.add_argument("--only", action="append", choices=sorted(LOADERS),
                   help="Load only these entities (repeatable)")
    args = p.parse_args(argv)
    result = load_all(args.dir, args.only)
    log.info("garmin.load.done", **result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
