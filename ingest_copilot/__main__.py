"""ingest_copilot CLI.

Usage:
    # one-time bootstrap (paste a Firebase refresh token captured from your
    # browser — see ingest_copilot/auth.py docstring for how to grab it):
    python -m ingest_copilot set-refresh-token --token AMf-...

    # ongoing:
    python -m ingest_copilot ingest                       # 35-day window
    python -m ingest_copilot ingest --backfill 1825       # 5-year backfill
    python -m ingest_copilot ingest --data-type accounts  # just one source
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_copilot import auth, ingest
from lifeos_core.logging import configure_logging


def _cmd_set_refresh_token(args) -> int:
    auth.store_refresh_token(args.token)
    print("OK. Refresh token stored. Try: python -m ingest_copilot ingest")
    return 0


def _cmd_ingest(args) -> int:
    if args.data_type == "transactions":
        n = ingest.ingest_transactions(backfill_days=args.backfill)
        print(json.dumps({"transactions": n}))
        return 0
    if args.data_type == "categories":
        print(json.dumps({"categories": ingest.ingest_categories()}))
        return 0
    if args.data_type == "accounts":
        print(json.dumps({"accounts": ingest.ingest_accounts()}))
        return 0

    out = ingest.run_all(backfill_days=args.backfill)
    print(json.dumps(out, indent=2, default=str))
    return 1 if any(isinstance(v, str) for v in out.values()) else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_copilot")
    sub = p.add_subparsers(dest="cmd", required=True)

    tok = sub.add_parser("set-refresh-token", help="One-time: store a Firebase refresh token.")
    tok.add_argument("--token", required=True, help="Refresh token starting with AMf-")
    tok.set_defaults(func=_cmd_set_refresh_token)

    ing = sub.add_parser("ingest")
    ing.add_argument("--backfill", type=int, default=None)
    ing.add_argument(
        "--data-type",
        choices=["transactions", "categories", "accounts"],
        default=None,
    )
    ing.set_defaults(func=_cmd_ingest)

    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
