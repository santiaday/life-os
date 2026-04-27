"""ingest_cronometer CLI.

Usage:
    python -m ingest_cronometer ingest                   # all data types, incremental
    python -m ingest_cronometer ingest --backfill 365    # full re-pull
    python -m ingest_cronometer ingest --data-type servings
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_cronometer import ingest
from lifeos_core.logging import configure_logging


def _cmd(args) -> int:
    if args.data_type:
        fn_map = {
            "servings": ingest.ingest_servings,
            "daily-nutrition": ingest.ingest_daily_nutrition,
            "biometrics": ingest.ingest_biometrics,
            "exercises": ingest.ingest_exercises,
        }
        fn = fn_map[args.data_type]
        count = fn(backfill_days=args.backfill)
        print(json.dumps({args.data_type: count}))
        return 0

    out = ingest.run_all(backfill_days=args.backfill)
    print(json.dumps(out, indent=2, default=str))
    failures = [k for k, v in out.items() if isinstance(v, str)]
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_cronometer")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--backfill", type=int, default=None)
    ing.add_argument(
        "--data-type",
        choices=["servings", "daily-nutrition", "biometrics", "exercises"],
        default=None,
    )
    ing.set_defaults(func=_cmd)

    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
