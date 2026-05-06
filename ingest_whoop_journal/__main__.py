"""ingest_whoop_journal CLI.

Usage:
    # one-time bootstrap (parse a mitmproxy capture, persist tokens, write
    # sidecar refresh-token file the iPhone Shortcut reads):
    python -m ingest_whoop_journal.bootstrap_from_capture path/to/Mitmproxy_Flows

    # ongoing (the iPhone Shortcut keeps the token table fresh; this just
    # consumes it):
    python -m ingest_whoop_journal                          # 2-day window
    python -m ingest_whoop_journal --backfill 7             # last 8 days
    python -m ingest_whoop_journal --start 2024-01-01 \\
                                   --end   2024-12-31       # explicit range
    python -m ingest_whoop_journal --data-type catalog
    python -m ingest_whoop_journal --day 2026-04-27         # one specific day

Default mode is ``ingest`` (no subcommand needed) so the scheduler can
invoke ``python -m ingest_whoop_journal`` cleanly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from ingest_whoop_journal import ingest
from ingest_whoop_journal.auth import WhoopAuth
from ingest_whoop_journal.client import WhoopJournalClient
from lifeos_core.logging import configure_logging


def _run(args) -> int:
    if args.data_type == "catalog":
        n = ingest.ingest_behavior_catalog()
        print(json.dumps({"behavior_catalog": n}))
        return 0

    if args.day:
        d = date.fromisoformat(args.day)
        a = WhoopAuth()
        a.ensure_fresh()
        with WhoopJournalClient(auth=a) as client:
            out = ingest.ingest_journal_day(d, client=client)
        print(json.dumps({args.day: out}))
        return 0

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if (start is None) != (end is None):
        print("ERROR: --start and --end must be provided together.", file=sys.stderr)
        return 2

    out = ingest.run_all(backfill_days=args.backfill, start=start, end=end)
    print(json.dumps(out, indent=2, default=str))
    return 1 if any(isinstance(v, str) for v in out.values()) else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_whoop_journal")
    p.add_argument(
        "--backfill", type=int, default=None,
        help="Days back to fetch (today + N). Omit for default 2-day window.",
    )
    p.add_argument(
        "--start", type=str, default=None,
        help="YYYY-MM-DD. Inclusive start of an explicit range. Pair with --end.",
    )
    p.add_argument(
        "--end", type=str, default=None,
        help="YYYY-MM-DD. Inclusive end of an explicit range. Pair with --start.",
    )
    p.add_argument(
        "--day", type=str, default=None,
        help="Single YYYY-MM-DD to fetch. Skips the run_all wrapper.",
    )
    p.add_argument(
        "--data-type", choices=["catalog", "drafts"], default=None,
    )

    args = p.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
