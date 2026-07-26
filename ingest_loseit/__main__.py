"""Ongoing Lose It! sync.

    python -m ingest_loseit ingest              download the export, load, unify
    python -m ingest_loseit ingest --keep DIR   keep the extracted CSVs for inspection
    python -m ingest_loseit check               verify the session cookie only

Runs every two hours from the scheduler. The whole job is: download the
current CSV export, hand it to the same loader the manual import used, and
re-project. Every step is an upsert on a stable natural key, so running it
often is free -- days that haven't changed rewrite identical rows.

Food logging in Lose It stopped 2025-09-05, but weigh-ins have continued; as
of the 2026-07-25 export it is the freshest weight source in the warehouse.
That is what this job is really for.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from ingest_files.loseit import load_all
from ingest_loseit.client import LoseItAuthError, LoseItClient, LoseItError
from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run

log = get_logger(__name__)


def ingest(keep_dir: str | None = None) -> dict:
    tmp = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="loseit-"))
    try:
        with ingestion_run("loseit", "export") as run:
            with LoseItClient() as client:
                csv_dir = client.download_to(tmp)
            counts = load_all(str(csv_dir))
            total = sum(counts.values())
            run.fetched(total)
            run.upserted(total)
            run.add_metadata(**counts)

        # Re-project only the Lose It slice; a full unify run is the
        # scheduler's job, not this one's.
        with ingestion_run("loseit", "unify") as run, tx() as conn:
            from unify.sources import loseit as project

            projected = project.project_all(conn)
            run.upserted(sum(v for v in projected.values() if isinstance(v, int)))
        return {"loaded": counts, "projected": projected}
    finally:
        if not keep_dir:
            shutil.rmtree(tmp, ignore_errors=True)


def check() -> dict:
    with LoseItClient() as client:
        blob = client.fetch_export()
    return {"ok": True, "bytes": len(blob)}


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(description="Sync Lose It! via the data export.")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("--keep", help="Directory to keep the extracted CSVs in.")
    sub.add_parser("check")
    args = p.parse_args(argv)

    try:
        result = ingest(args.keep) if args.cmd == "ingest" else check()
    except LoseItAuthError as e:
        # Actionable and non-fatal: the scheduler keeps running, and
        # source_health will show loseit going stale.
        log.error("loseit.auth_expired", error=str(e))
        print(f"AUTH: {e}", file=sys.stderr)
        return 2
    except LoseItError as e:
        log.error("loseit.failed", error=str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
