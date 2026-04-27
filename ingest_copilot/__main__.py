"""ingest_copilot CLI."""

from __future__ import annotations

import argparse
import json
import sys

from ingest_copilot import ingest
from lifeos_core.logging import configure_logging


def _cmd(args) -> int:
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
    ing = sub.add_parser("ingest")
    ing.add_argument("--backfill", type=int, default=None)
    ing.add_argument(
        "--data-type",
        choices=["transactions", "categories", "accounts"],
        default=None,
    )
    ing.set_defaults(func=_cmd)
    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
