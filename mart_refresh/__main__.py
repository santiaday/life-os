"""mart_refresh CLI entrypoint.

Usage:
    python -m mart_refresh                # rebuild all marts
    python -m mart_refresh --table daily  # rebuild a single mart
"""

from __future__ import annotations

import argparse
import json
import sys

from lifeos_core.logging import configure_logging
from mart_refresh import refresh as r


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="mart_refresh")
    p.add_argument(
        "--table",
        choices=["daily", "meal", "weekly", "all"],
        default="all",
    )
    args = p.parse_args(argv)

    if args.table == "all":
        out = r.refresh_all()
        print(json.dumps(out, indent=2, default=str))
        failures = [k for k, v in out.items() if isinstance(v, dict) and "error" in v]
        return 1 if failures else 0

    fn = {
        "daily": r.refresh_mart_daily,
        "meal": r.refresh_mart_meal,
        "weekly": r.refresh_mart_weekly,
    }[args.table]
    rows = fn()
    print(json.dumps({f"mart_{args.table}": {"rows": rows}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
