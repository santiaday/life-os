"""ingest_whoop_journal CLI.

Usage:
    python -m ingest_whoop_journal ingest                   # 3-day window
    python -m ingest_whoop_journal ingest --backfill 365    # backfill 1 year
    python -m ingest_whoop_journal ingest --data-type catalog
    python -m ingest_whoop_journal ingest --day 2026-04-27  # one specific day

Auth bootstrap is automatic — first call uses WHOOP_PRIVATE_EMAIL +
WHOOP_PRIVATE_PASSWORD from .env via Cognito password flow, persists tokens
to oauth_tokens. Subsequent calls use refresh-token flow.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from ingest_whoop_journal import ingest
from lifeos_core.logging import configure_logging


def _cmd(args) -> int:
    if args.data_type == "catalog":
        n = ingest.ingest_behavior_catalog()
        print(json.dumps({"behavior_catalog": n}))
        return 0
    if args.day:
        out = ingest.ingest_journal_day(date.fromisoformat(args.day))
        print(json.dumps({args.day: out}))
        return 0

    out = ingest.run_all(backfill_days=args.backfill)
    print(json.dumps(out, indent=2, default=str))
    return 1 if any(isinstance(v, str) for v in out.values()) else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_whoop_journal")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--backfill", type=int, default=None,
                     help="Days back to fetch. Omit for 3-day rolling window.")
    ing.add_argument("--day", type=str, default=None,
                     help="Single YYYY-MM-DD to fetch. Skips run_all wrapper.")
    ing.add_argument("--data-type", choices=["catalog", "drafts"], default=None)
    ing.set_defaults(func=_cmd)

    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
