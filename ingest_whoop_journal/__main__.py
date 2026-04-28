"""ingest_whoop_journal CLI.

Usage:
    # one-time bootstrap: capture refresh token from your Whoop browser
    # session (https://app.prod.whoop.com → DevTools → Application → Local
    # Storage → CognitoIdentityServiceProvider.*.refreshToken) then:
    python -m ingest_whoop_journal set-refresh-token --token <paste>

    # ongoing:
    python -m ingest_whoop_journal ingest                   # 3-day window
    python -m ingest_whoop_journal ingest --backfill 365    # backfill 1 year
    python -m ingest_whoop_journal ingest --data-type catalog
    python -m ingest_whoop_journal ingest --day 2026-04-27  # one specific day
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from ingest_whoop_journal import auth, ingest
from lifeos_core.logging import configure_logging


def _cmd_set_refresh_token(args) -> int:
    auth.store_refresh_token(args.token)
    print("OK. Refresh token stored. Try: python -m ingest_whoop_journal ingest")
    return 0


def _cmd_ingest(args) -> int:
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

    tok = sub.add_parser("set-refresh-token", help="One-time: store a captured Cognito refresh token.")
    tok.add_argument("--token", required=True, help="Refresh token from Whoop browser session localStorage")
    tok.set_defaults(func=_cmd_set_refresh_token)

    ing = sub.add_parser("ingest")
    ing.add_argument("--backfill", type=int, default=None,
                     help="Days back to fetch. Omit for 3-day rolling window.")
    ing.add_argument("--day", type=str, default=None,
                     help="Single YYYY-MM-DD to fetch. Skips run_all wrapper.")
    ing.add_argument("--data-type", choices=["catalog", "drafts"], default=None)
    ing.set_defaults(func=_cmd_ingest)

    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
