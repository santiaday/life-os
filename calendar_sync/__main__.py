"""CLI entrypoint for calendar_sync.

Subcommands:
    sync                 One-shot sync pass. Used by the scheduler.
    sync --limit N       Cap rows processed in this pass (debugging).
    config               Print resolved category→calendar map (sanity check).

The scheduler invokes `python -m calendar_sync sync` every 15 min. There's
no oauth subcommand here — auth is shared with ingest_calendar; run
`python -m ingest_calendar oauth-init` once to bootstrap a token with both
read and write scopes.
"""

from __future__ import annotations

import argparse
import json
import sys

from calendar_sync import sync as sync_mod
from lifeos_core.logging import configure_logging
from lifeos_core.settings import settings


def _cmd_sync(args) -> int:
    result = sync_mod.sync_once(limit=args.limit) if args.limit else sync_mod.run()
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("errors") else 0


def _cmd_config(_args) -> int:
    cfg = settings.lifelog_calendar_map
    if not cfg:
        print("LIFELOG_CALENDAR_MAP_JSON is empty.")
        return 1
    print(json.dumps(cfg, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="calendar_sync")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="Push pending events → Google Calendar.")
    s.add_argument("--limit", type=int, default=None,
                   help="Cap rows processed (debugging). Default uses LIFELOG_SYNC_BATCH_SIZE.")
    s.set_defaults(func=_cmd_sync)

    c = sub.add_parser("config", help="Print resolved category→calendar map.")
    c.set_defaults(func=_cmd_config)

    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["sync"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
