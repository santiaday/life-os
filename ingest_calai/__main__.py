"""CLI for the Cal AI ingester.

    python -m ingest_calai login --refresh-token <RT> [--user-id <UID>]
    python -m ingest_calai ingest [--backfill 30]

`login` stores a Firebase refresh token captured from a Cal AI sign-in (the Web
API key goes in CALAI_FIREBASE_API_KEY). `ingest` pulls the diary window from
Firestore and upserts it. See RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_calai import ingest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingest_calai", description="Cal AI nutrition ingester.")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("ingest", help="pull + upsert the diary window")
    pi.add_argument("--backfill", type=int, default=7, help="days back to fetch (default 7)")

    pl = sub.add_parser("login", help="store a Firebase refresh token")
    pl.add_argument("--refresh-token", required=True)
    pl.add_argument("--user-id", default=None)

    args = p.parse_args(argv)
    try:
        if args.cmd == "login":
            ingest.login(args.refresh_token, args.user_id)
            print(json.dumps({"ok": True, "stored": "calai refresh token"}))
        else:  # default to ingest
            print(json.dumps(ingest.run_all(getattr(args, "backfill", 7) or 7), default=str))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "type": type(e).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
