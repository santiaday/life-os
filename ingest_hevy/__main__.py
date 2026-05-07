"""CLI entrypoint for ingest_hevy.

Subcommands:
    ingest                       Default: incremental sync of changed workouts.
        --backfill DAYS            Re-pull every workout updated in the last N days.
        --catalog                  Also refresh dim_hevy_exercise on this run.
    catalog                      One-shot: refresh the exercise template catalog only.

Run:
    python -m ingest_hevy ingest
    python -m ingest_hevy ingest --backfill 30
    python -m ingest_hevy ingest --catalog
    python -m ingest_hevy catalog
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_hevy import ingest
from ingest_hevy.client import HevyClient
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)


def _cmd_ingest(args) -> int:
    results = ingest.run_all(
        backfill_days=args.backfill,
        refresh_catalog=args.catalog,
    )
    print(json.dumps(results, indent=2, default=str))
    failures = [k for k, v in results.items() if isinstance(v, str)]
    return 1 if failures else 0


def _cmd_catalog(_args) -> int:
    with HevyClient() as client:
        n = ingest.ingest_exercise_templates(client)
    print(json.dumps({"exercise_templates": n}))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_hevy")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Run an ingestion pass (default incremental).")
    ing.add_argument("--backfill", type=int, default=None,
                     help="Days of workouts to re-pull (instead of since-last).")
    ing.add_argument("--catalog", action="store_true",
                     help="Also refresh the exercise template catalog this run.")
    ing.set_defaults(func=_cmd_ingest)

    cat = sub.add_parser("catalog", help="Refresh dim_hevy_exercise only.")
    cat.set_defaults(func=_cmd_catalog)

    # Backwards-compat: bare `python -m ingest_hevy` runs `ingest` with defaults.
    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
