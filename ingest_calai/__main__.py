"""CLI for the Cal AI ingester.

    python -m ingest_calai login --refresh-token <RT> [--user-id <UID>]
    python -m ingest_calai ingest [--backfill 30]
    python -m ingest_calai local --sqlite /path/to/Model.sqlite [--user-id <UID>]

`login` stores a Firebase refresh token captured from a Cal AI sign-in (the Web
API key goes in CALAI_FIREBASE_API_KEY). `ingest` pulls the diary window from
Firestore and upserts it. `local` backfills from Cal AI's on-device CoreData
store (Model.sqlite, extracted from an iOS backup) — the authoritative diary.
See RUNBOOK.md.
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

    ploc = sub.add_parser("local", help="backfill from Cal AI's on-device CoreData store")
    g = ploc.add_mutually_exclusive_group(required=True)
    g.add_argument("--sqlite", help="path to an already-extracted Model.sqlite")
    g.add_argument("--from-backup", action="store_true",
                   help="auto-locate + extract Model.sqlite from the newest iOS backup")
    ploc.add_argument("--user-id", default=None)

    args = p.parse_args(argv)
    try:
        if args.cmd == "login":
            ingest.login(args.refresh_token, args.user_id)
            print(json.dumps({"ok": True, "stored": "calai refresh token"}))
        elif args.cmd == "local":
            from ingest_calai.local_db import run_local
            print(json.dumps(run_local(args.sqlite, from_backup=args.from_backup,
                                       user_id=args.user_id), default=str))
        else:  # default to ingest
            print(json.dumps(ingest.run_all(getattr(args, "backfill", 7) or 7), default=str))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "type": type(e).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
