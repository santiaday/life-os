"""CLI entrypoint for ingest_whoop.

Subcommands:
    oauth-init                Print the Whoop authorize URL.
    oauth-exchange --code X   Exchange an auth code for refresh+access tokens.
    ingest                    Default: incremental fetch of all data types.
        --backfill DAYS         Re-fetch the last N days (instead of since-last).
        --data-type X           Limit to one of cycle|recovery|sleep|workout|profile.

Run:
    python -m ingest_whoop ingest
    python -m ingest_whoop ingest --backfill 365
    python -m ingest_whoop oauth-init
"""

from __future__ import annotations

import argparse
import json
import sys

from ingest_whoop import ingest, oauth
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)


def _cmd_oauth_init(args) -> int:
    url = oauth.authorize_url(state=args.state)
    print("Visit this URL, approve, and copy the `code` from the redirect URL:")
    print()
    print(url)
    print()
    print("Then run: python -m ingest_whoop oauth-exchange --code <code>")
    return 0


def _cmd_oauth_exchange(args) -> int:
    tok = oauth.exchange_code(args.code)
    expires = tok.get("expires_in", "?")
    print(f"OK. Stored refresh + access token. expires_in={expires}s")
    return 0


def _cmd_ingest(args) -> int:
    if args.data_type:
        fn_map = {
            "cycle": ingest.ingest_cycles,
            "recovery": ingest.ingest_recoveries,
            "sleep": ingest.ingest_sleep,
            "workout": ingest.ingest_workouts,
            "profile": ingest.ingest_profile,
        }
        fn = fn_map[args.data_type]
        from ingest_whoop.client import WhoopClient

        with WhoopClient() as client:
            if args.data_type == "profile":
                count = fn(client)
            else:
                count = fn(client, backfill_days=args.backfill)
        print(json.dumps({args.data_type: count}))
        return 0

    results = ingest.run_all(backfill_days=args.backfill)
    print(json.dumps(results, indent=2, default=str))
    failures = [k for k, v in results.items() if isinstance(v, str)]
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="ingest_whoop")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("oauth-init", help="Print authorize URL.")
    init.add_argument("--state", default="lifeos")
    init.set_defaults(func=_cmd_oauth_init)

    exch = sub.add_parser("oauth-exchange", help="Exchange code for refresh token.")
    exch.add_argument("--code", required=True)
    exch.set_defaults(func=_cmd_oauth_exchange)

    ing = sub.add_parser("ingest", help="Run an ingestion pass (default incremental).")
    ing.add_argument("--backfill", type=int, default=None, help="Days to back-fill.")
    ing.add_argument(
        "--data-type",
        choices=["cycle", "recovery", "sleep", "workout", "profile"],
        default=None,
    )
    ing.set_defaults(func=_cmd_ingest)

    # Backwards-compat: bare `python -m ingest_whoop` runs `ingest` with defaults.
    args = p.parse_args(argv) if argv is not None or len(sys.argv) > 1 else p.parse_args(["ingest"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
