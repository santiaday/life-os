"""ingest_whoop_labs CLI.

Whoop Advanced Labs has no public API yet. Capture the JSON payload from the
mobile app's network tab (the GET response when you open a panel result),
save it to a file, and feed it here.

Usage:
    # seed / refresh the curated biomarker reference data
    python -m ingest_whoop_labs seed-catalog

    # ingest a panel JSON dump
    python -m ingest_whoop_labs ingest --file whoop_labs.txt

    # both at once
    python -m ingest_whoop_labs run-all --file whoop_labs.txt
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_whoop_labs import ingest
from lifeos_core.logging import configure_logging


def _cmd_seed_catalog(_args) -> int:
    n = ingest.ingest_biomarker_catalog()
    print(json.dumps({"biomarker_catalog": n}, indent=2))
    return 0


def _cmd_ingest(args) -> int:
    out = ingest.ingest_lab_panel(args.file)
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_run_all(args) -> int:
    out = ingest.run_all(args.file)
    print(json.dumps(out, indent=2, default=str))
    return 1 if any(isinstance(v, str) and v.startswith("FAILED") for v in out.values()) else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_whoop_labs")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("seed-catalog", help="Upsert dim_lab_biomarker from the curated reference data.")
    sc.set_defaults(func=_cmd_seed_catalog)

    ing = sub.add_parser("ingest", help="Ingest one panel JSON dump.")
    ing.add_argument("--file", required=True, help="Path to a Whoop labs JSON file.")
    ing.set_defaults(func=_cmd_ingest)

    ra = sub.add_parser("run-all", help="Seed catalog + ingest panel.")
    ra.add_argument("--file", default=None, help="Path to a panel JSON; omit to just re-seed catalog.")
    ra.set_defaults(func=_cmd_run_all)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
