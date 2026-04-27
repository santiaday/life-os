"""CLI entrypoint for ingest_calendar.

Subcommands:
    oauth-init                    Print the Google authorize URL.
    oauth-exchange --code X       Exchange auth code for refresh token.
    ingest [--force-full]         Run incremental sync for every calendar in
                                  GOOGLE_CALENDAR_IDS. With --force-full,
                                  ignore stored syncToken and do a full re-sync.
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_calendar import ingest, oauth
from lifeos_core.logging import configure_logging


def _cmd_oauth_init(args) -> int:
    print("Visit:")
    print()
    print(oauth.authorize_url(state=args.state))
    print()
    print("Then: python -m ingest_calendar oauth-exchange --code <code>")
    return 0


def _cmd_oauth_exchange(args) -> int:
    out = oauth.exchange_code(args.code)
    print(f"OK. expires_at={out.get('expires_at')}")
    return 0


def _cmd_ingest(args) -> int:
    results = ingest.run_all(force_full=args.force_full)
    print(json.dumps(results, indent=2, default=str))
    failures = [k for k, v in results.items() if isinstance(v, str)]
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_calendar")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("oauth-init")
    init.add_argument("--state", default="lifeos-bootstrap")
    init.set_defaults(func=_cmd_oauth_init)

    exch = sub.add_parser("oauth-exchange")
    exch.add_argument("--code", required=True)
    exch.set_defaults(func=_cmd_oauth_exchange)

    ing = sub.add_parser("ingest")
    ing.add_argument("--force-full", action="store_true",
                     help="Ignore syncToken; force a full window re-sync.")
    ing.set_defaults(func=_cmd_ingest)

    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
